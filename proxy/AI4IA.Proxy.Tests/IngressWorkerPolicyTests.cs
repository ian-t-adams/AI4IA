using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using SimpleL7Proxy;
using SimpleL7Proxy.Async.BlobStorage;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Backend.Iterators;
using SimpleL7Proxy.Config;
using SimpleL7Proxy.Events;
using SimpleL7Proxy.Plugin;
using SimpleL7Proxy.Proxy;
using SimpleL7Proxy.Queue;
using SimpleL7Proxy.StreamProcessor;
using SimpleL7Proxy.User;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Runs the real listener, priority queue and worker loop against a loopback backend, so
/// the ingress header policy and the worker's requeue handling are exercised exactly where
/// production applies them rather than through a direct call into the send loop.
/// </summary>
[TestClass]
[DoNotParallelize] // The vendored worker, queue and stream logger are static.
public sealed class IngressWorkerPolicyTests
{
    private const string IngressKey = "fixture-ingress-key";
    private const string ModelPath = NoReplayWorkerTests.Path;

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task AuthoredPolicyStripsModelOverrideBeforeTheWorkerRewritesTheBody(bool authoredPolicy)
    {
        await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var gateway = await Gateway.Start(backend, authoredPolicy);
        using var response = await gateway.Post(ModelPath, NoReplayWorkerTests.Body, new()
        {
            ["S7P-Model-Override"] = "fixture-override",
        });
        Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
        var sent = backend.Requests.Single();
        // Control: without the authored policy the upstream header rewrites the admitted model.
        Assert.AreEqual(!authoredPolicy, Encoding.UTF8.GetString(sent.Body).Contains("fixture-override"));
        Assert.AreEqual(authoredPolicy ? "fixture-text" : "fixture-override", sent.Headers["x-LLMModel"]);
        if (authoredPolicy)
            CollectionAssert.AreEqual(NoReplayWorkerTests.Body, sent.Body);
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task AuthoredPolicyKeepsResponseLineCaptureOff(bool authoredPolicy)
    {
        const string usage = """{"id":"fixture","usage":{"prompt_tokens":1,"total_tokens":2}}""";
        await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200, Body: usage, Chunked: true,
            Headers: new(StringComparer.OrdinalIgnoreCase) { ["TOKENPROCESSOR"] = ["MultiLineAllUsage"] })));
        var captured = new RecordingLogger();
        BaseStreamProcessor.SetLogger(captured);
        try
        {
            await using var gateway = await Gateway.Start(backend, authoredPolicy);
            using var response = await gateway.Post(ModelPath, NoReplayWorkerTests.Body, new()
            {
                ["S7PDEBUGSTREAM"] = "true",
            });
            Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
            StringAssert.Contains(await response.Content.ReadAsStringAsync(), "prompt_tokens");
        }
        finally
        {
            BaseStreamProcessor.SetLogger(NullLogger.Instance);
        }
        // Control: without the authored policy the upstream header logs model output lines.
        Assert.AreEqual(!authoredPolicy, captured.Messages.Any(message => message.StartsWith("Captured line")));
        Assert.AreEqual(!authoredPolicy, captured.Messages.Any(message => message.Contains("prompt_tokens")));
    }

    [DataTestMethod]
    [DataRow(false)]
    [DataRow(true)]
    public async Task OpenCircuitRequeuesOrdinaryWorkButTheWorkerRefusesTheBoundedAttempt(bool bounded)
    {
        await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200)));
        // Blocked for the first dispatch only, so a requeued ordinary request can finish.
        await using var gateway = await Gateway.Start(
            backend, authoredPolicy: true, bounded: bounded, circuit: () => new NoReplayWorkerTests.OpenCircuit(50, 1));
        var headers = new Dictionary<string, string>();
        if (bounded)
            headers[NoReplayAttempt.RequestHeader] = NoReplayWorkerTests.Header(NoReplayWorkerTests.Body);
        using var response = await gateway.Post(
            bounded ? NoReplayWorkerTests.BoundedPath : ModelPath, NoReplayWorkerTests.Body, headers);
        if (bounded)
        {
            Assert.AreEqual(HttpStatusCode.BadRequest, response.StatusCode);
            StringAssert.Contains(await response.Content.ReadAsStringAsync(), "cannot be persisted or requeued");
            Assert.AreEqual(0, gateway.Queue.Requeues);
            Assert.AreEqual(0, backend.Requests.Count);
        }
        else
        {
            // Control: the same open circuit makes the real worker requeue ordinary work once.
            Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
            Assert.AreEqual(1, gateway.Queue.Requeues);
            Assert.AreEqual(1, backend.Requests.Count);
        }
    }

    private sealed class Gateway : IAsyncDisposable
    {
        private readonly CancellationTokenSource _stop = new();
        private readonly HttpClient _caller = new(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false })
        {
            Timeout = TimeSpan.FromSeconds(15),
        };
        private Server _server = null!;
        private Task _worker = Task.CompletedTask;
        private RequeueDelayWorker _requeue = null!;
        private SharedIteratorRegistry _registry = null!;
        private ProxyConfig _options = null!;
        private string _url = null!;
        internal SpyQueue Queue { get; private set; } = null!;

        internal static async Task<Gateway> Start(
            WireServer backend, bool authoredPolicy, bool bounded = false, Func<ICircuitBreaker>? circuit = null)
        {
            var gateway = new Gateway();
            var options = gateway._options = new ProxyConfig
            {
                Client = new HttpClient(new SocketsHttpHandler { UseProxy = false }),
                LoadBalanceMode = Constants.Latency,
                IterationMode = IterationModeEnum.SinglePass,
                UseProfiles = false,
                Timeout = 5000,
                TrackWorkers = false,
                Workers = 1,
                DefaultPriority = 2,
                ValidateAuthConfig = "enabled=true;mode=key;header=S7P-KEY",
                ValidateAuthKey1 = IngressKey,
                StripRequestHeaders = ["S7P-KEY"],
                DisallowedHeaders = authoredPolicy ? GatewayUpstreamPolicyTests.AuthoredDisallowedHeaders() : [],
            };
            typeof(HealthCheckService).GetField("_options", BindingFlags.NonPublic | BindingFlags.Static)!
                .SetValue(null, options);
            string routing = bounded
                ? $"path={NoReplayAttempt.RoutePrefix};stripprefix=false;retryafter=false;api-key={NoReplayWorkerTests.Key}"
                : $"retryafter=false;api-key={NoReplayWorkerTests.LegacyKey}";
            var config = new HostConfig($"host={backend.Url};mode=apim;api-key-header=Ocp-Apim-Subscription-Key;{routing}");
            NoReplayWorkerTests.SetCircuit(config, circuit?.Invoke());
            var hosts = new List<BaseHostHealth> { new NoReplayWorkerTests.FixtureHost(config) };
            var backends = new NoReplayWorkerTests.Backends(hosts);
            var wrapped = Options.Create(options);
            gateway.Queue = new SpyQueue(new ConcurrentPriQueue<RequestData>(wrapped, NullLogger<ConcurrentPriQueue<RequestData>>.Instance));
            gateway._requeue = new RequeueDelayWorker(NullLogger<RequeueDelayWorker>.Instance, gateway.Queue);
            var notifier = new ConfigChangeNotifier(NullLogger<ConfigChangeNotifier>.Instance);
            gateway._registry = new SharedIteratorRegistry(NullLogger<SharedIteratorRegistry>.Instance, options, notifier);
            var profiles = DispatchProxy.Create<IUserProfileService, NoReplayWorkerTests.UnusedDependency>();
            var context = new WorkerContext(
                options, gateway.Queue, backends, new UserPriority(), profiles, gateway._requeue,
                DispatchProxy.Create<IEventClient, NoReplayWorkerTests.UnusedDependency>(), NullLogger<ProxyWorker>.Instance,
                new StreamProcessorFactory(NullLogger<StreamProcessorFactory>.Instance),
                new RequestLifecycleManager(NullLogger<RequestLifecycleManager>.Instance, wrapped),
                new EventDataBuilder(NullLogger<EventDataBuilder>.Instance, wrapped),
                (HealthCheckService)RuntimeHelpers.GetUninitializedObject(typeof(HealthCheckService)),
                notifier, new StreamFlusher(options), sharedIteratorRegistry: gateway._registry);
            var worker = new ProxyWorker(0, options.DefaultPriority, context, gateway._stop.Token);

            gateway._server = new Server(
                gateway.Queue, wrapped,
                DispatchProxy.Create<IHostApplicationLifetime, NoReplayWorkerTests.UnusedDependency>(),
                new UserPriority(), profiles,
                new ProfileEnricher(options, profiles, NullLogger<ProfileEnricher>.Instance),
                null, backends, Array.Empty<IRequestPreprocessorPlugin>(),
                new NullBlobWriter(NullLogger<NullBlobWriter>.Instance),
                (HealthCheckService)RuntimeHelpers.GetUninitializedObject(typeof(HealthCheckService)),
                (ProbeServer)RuntimeHelpers.GetUninitializedObject(typeof(ProbeServer)),
                notifier, NullLogger<Server>.Instance);
            // The production prefix binds every interface; the harness binds loopback only.
            using var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            int port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            var listener = (HttpListener)typeof(Server)
                .GetField("_httpListener", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(gateway._server)!;
            listener.Prefixes.Clear();
            listener.Prefixes.Add($"http://127.0.0.1:{port}/");
            gateway._url = $"http://127.0.0.1:{port}";
            await gateway._server.StartAsync(gateway._stop.Token);
            gateway._worker = Task.Run(worker.TaskRunnerAsync);
            return gateway;
        }

        internal Task<HttpResponseMessage> Post(string path, byte[] body, Dictionary<string, string> headers)
        {
            var request = new HttpRequestMessage(HttpMethod.Post, _url + path) { Content = new ByteArrayContent(body) };
            request.Content.Headers.ContentType = new("application/json");
            request.Headers.TryAddWithoutValidation("S7P-KEY", IngressKey);
            foreach (var (name, value) in headers)
                request.Headers.TryAddWithoutValidation(name, value);
            return _caller.SendAsync(request);
        }

        public async ValueTask DisposeAsync()
        {
            _stop.Cancel();
            try { await _server.StopProbes(CancellationToken.None); }
            catch (ObjectDisposedException) { }
            await Queue.StopAsync();
            try { await _worker.WaitAsync(TimeSpan.FromSeconds(5)); }
            catch (Exception error) when (error is OperationCanceledException or TimeoutException) { }
            _caller.Dispose();
            _requeue.Dispose();
            _registry.Dispose();
            _options.Client!.Dispose();
            _stop.Dispose();
        }
    }

    internal sealed class SpyQueue(ConcurrentPriQueue<RequestData> inner) : IConcurrentPriQueue<RequestData>
    {
        private int _requeues;
        public int Requeues => Volatile.Read(ref _requeues);
        public int MaxQueueLength => inner.MaxQueueLength;
        public int thrdSafeCount => inner.thrdSafeCount;
        public bool Enqueue(RequestData item, int priority, int priority2, DateTime timestamp, bool allowOverflow = false) =>
            inner.Enqueue(item, priority, priority2, timestamp, allowOverflow);
        public bool Requeue(RequestData item, int priority, int priority2, DateTime timestamp)
        {
            Interlocked.Increment(ref _requeues);
            return inner.Requeue(item, priority, priority2, timestamp);
        }
        public Task<RequestData> DequeueAsync(int preferredPriority) => inner.DequeueAsync(preferredPriority);
        public Task StopAsync() => inner.StopAsync();
        public void StartSignaler(CancellationToken token) => inner.StartSignaler(token);
        public Task SignalWorker(CancellationToken token) => inner.SignalWorker(token);
    }

    private sealed class UserPriority : IUserPriorityService
    {
        public float threshold { get; set; }
        public string GetState() => "fixture";
        public Guid addRequest(string userId) => Guid.NewGuid();
        public void addRequest(Guid requestId, string userId) { }
        public bool removeRequest(string userId, Guid requestId) => true;
        public bool boostIndicator(string userId, out float boostValue)
        {
            boostValue = 0;
            return false;
        }
    }

    private sealed class RecordingLogger : ILogger
    {
        private readonly ConcurrentQueue<string> _messages = new();
        public IReadOnlyCollection<string> Messages => _messages;
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel logLevel) => true;
        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception,
            Func<TState, Exception?, string> formatter) => _messages.Enqueue(formatter(state, exception));
    }
}
