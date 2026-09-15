using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using SimpleL7Proxy;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Backend.Iterators;
using SimpleL7Proxy.Config;
using SimpleL7Proxy.DTO;
using SimpleL7Proxy.Events;
using SimpleL7Proxy.Proxy;
using SimpleL7Proxy.Queue;
using SimpleL7Proxy.StreamProcessor;
using SimpleL7Proxy.User;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize] // The vendored worker's queue/header configuration is static.
public sealed class NoReplayWorkerTests
{
    internal const string Key = "fixture-key";
    internal const string LegacyKey = "fixture-legacy-key";
    internal const string Path = "/openai/deployments/fixture-text/chat/completions?api-version=fixture";
    internal const string BoundedPath = NoReplayAttempt.RoutePrefix + Path;
    internal static readonly byte[] Body = Encoding.UTF8.GetBytes(
        """{"messages":[{"role":"user","content":"hello \u263a"}]}""");

    [DataTestMethod]
    [DataRow(500, false)]
    [DataRow(500, true)]
    [DataRow(404, false)]
    [DataRow(404, true)]
    [DataRow(412, false)]
    [DataRow(412, true)]
    [DataRow(429, false)]
    [DataRow(429, true)]
    [DataRow(307, false)]
    [DataRow(307, true)]
    public async Task ActualHostSendsPreserveLegacyFailoverButBoundedRequestStops(int status, bool shared)
    {
        foreach (bool bounded in new[] { false, true })
        {
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var first = new WireServer(_ => Task.FromResult(new WireReply(
                status, Location: second.Url + Path)));
            await using var fixture = await WorkerFixture.Create([first, second], bounded, shared: shared);
            using var result = await fixture.Send();
            Assert.AreEqual(bounded ? status : 200, (int)result.StatusCode,
                System.Text.Json.JsonSerializer.Serialize(fixture.Request.incompleteRequests));
            Assert.AreEqual(bounded ? 1 : 2, first.Requests.Count + second.Requests.Count);
            if (bounded)
            {
                Assert.IsTrue(fixture.Request.NoReplay!.Claimed);
                Assert.AreEqual(0, second.Requests.Count);
                VerifySignature(first.Requests.Single());
                Assert.IsFalse(result.Headers.AllKeys.Contains("S7PREQUEUE"));
                // The dedicated HttpClient must remain alive beyond response headers.
                Assert.AreEqual("{}", await result.BodyResponseMessage!.Content.ReadAsStringAsync());
            }
        }
    }

    [DataTestMethod]
    [DataRow("timeout")]
    [DataRow("lost-response")]
    public async Task ActualTimeoutAndLostAcknowledgementNeverAuthorizeAnotherSend(string failure)
    {
        foreach (bool bounded in new[] { false, true })
        {
            await using var first = new WireServer(async _ =>
            {
                if (failure == "timeout")
                    await Task.Delay(500);
                return new WireReply(200, Drop: failure == "lost-response", AllowDisconnect: true);
            });
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var fixture = await WorkerFixture.Create([first, second], bounded, timeout: 100);
            using var result = await fixture.Send();
            Assert.AreEqual(bounded ? 1 : 2, first.Requests.Count + second.Requests.Count);
            Assert.AreEqual(bounded ? 0 : 1, second.Requests.Count);
            Assert.AreEqual(bounded ? (failure == "timeout" ? 408 : 502) : 200, (int)result.StatusCode);
        }
    }

    [TestMethod]
    public async Task MissingApimAcknowledgementNeverBecomesLegacyFailover()
    {
        await using var first = new WireServer(_ => Task.FromResult(new WireReply(200, Ack: false, AllowDisconnect: true)));
        await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var fixture = await WorkerFixture.Create([first, second], true);
        await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => fixture.Send());
        Assert.AreEqual(1, first.Requests.Count);
        Assert.AreEqual(0, second.Requests.Count);
    }

    [TestMethod]
    public async Task RealRequeueDelayResetsLegacyCountersButCannotResetBoundedClaim()
    {
        foreach (bool bounded in new[] { false, true })
        {
            int calls = 0;
            await using var server = new WireServer(_ => Task.FromResult(
                Interlocked.Increment(ref calls) == 1
                    ? new WireReply(429, Requeue: true) : new WireReply(200)));
            await using var fixture = await WorkerFixture.Create([server], bounded);
            if (bounded)
            {
                using var first = await fixture.Send();
                Assert.AreEqual(429, (int)first.StatusCode);
                Assert.ThrowsException<ProxyErrorException>(() => fixture.Requeue.DelayAsync(fixture.Request, 1));
                fixture.Request.BackendAttempts = fixture.Request.LifetimeBackendAttempts = 0;
                await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => fixture.Send());
                Assert.AreEqual(0, fixture.Queue.thrdSafeCount);
            }
            else
            {
                using var requeue = await Assert.ThrowsExceptionAsync<S7PRequeueException>(() => fixture.Send());
                fixture.Requeue.DelayAsync(fixture.Request, 1);
                await fixture.Queue.Enqueued.Task.WaitAsync(TimeSpan.FromSeconds(3));
                Assert.AreEqual(0, fixture.Request.BackendAttempts);
                fixture.Request.Requeued = false; // TaskRunnerAsync's next dequeue.
                using var second = await fixture.Send();
                Assert.AreEqual(HttpStatusCode.OK, second.StatusCode);
            }
            Assert.AreEqual(bounded ? 1 : 2, server.Requests.Count);
        }
    }

    [DataTestMethod]
    [DataRow("version")]
    [DataRow("hash")]
    [DataRow("body")]
    [DataRow("model")]
    [DataRow("path")]
    [DataRow("host-auth")]
    [DataRow("async")]
    [DataRow("recovery")]
    [DataRow("hosted-tool")]
    [DataRow("multimodal")]
    public async Task InvalidBindingIsRejectedBeforeFirstBackendSend(string defect)
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var fixture = await WorkerFixture.Create([server], false, versioned: true);
        var request = fixture.Request;
        if (defect == "hosted-tool")
            request.setBody(Encoding.UTF8.GetBytes("""{"messages":[{"role":"user","content":"hello"}],"tools":[{"type":"web_search"}]}"""));
        if (defect == "multimodal")
            request.setBody(Encoding.UTF8.GetBytes("""{"messages":[{"role":"user","content":[{"type":"image","source":{"url":"https://example.test"}}]}]}"""));
        request.Headers[NoReplayAttempt.RequestHeader] = Header(request.BodyBytes ?? Body);
        if (defect == "version")
            request.Headers[NoReplayAttempt.RequestHeader] = "unknown.1";
        if (defect == "hash")
            request.Headers[NoReplayAttempt.RequestHeader] = Header([]); // Well-formed, wrong bytes.
        if (defect is "version" or "async")
        {
            if (defect == "async")
                request.Headers[fixture.Options.AsyncClientRequestHeader] = "true";
            Assert.ThrowsException<ProxyErrorException>(() =>
                NoReplayAttempt.BindAuthenticated(request, true, fixture.Options));
        }
        else
        {
            NoReplayAttempt.BindAuthenticated(request, true, fixture.Options);
            switch (defect)
            {
                case "body": request.setBody(Encoding.UTF8.GetBytes("{}")); break;
                case "model": request.Model = "another-model"; break;
                case "path": request.Path = BoundedPath.Replace("fixture-text", "another-model"); break;
                case "recovery": request.AsyncHydrated = true; break;
                case "host-auth":
                    fixture.Hosts[0].Config = new HostConfig($"host={server.Url};mode=direct");
                    SetCircuit(fixture.Hosts[0].Config);
                    break;
            }
            await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => fixture.Send());
        }
        Assert.AreEqual(0, server.Requests.Count);
        // Same worker/endpoint and original valid bytes demonstrably send.
        await using var control = await WorkerFixture.Create([server], true);
        using var result = await control.Send();
        Assert.AreEqual(1, server.Requests.Count);
        Assert.AreEqual(HttpStatusCode.OK, result.StatusCode);
    }

    [TestMethod]
    public async Task AuthenticatedIngressCannotAcceptForgedProxyProofOrUnsignedSelection()
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
        foreach (bool authenticated in new[] { false, true })
        {
            await using var fixture = await WorkerFixture.Create([server], false, versioned: true);
            fixture.Request.Headers[NoReplayAttempt.RequestHeader] = Header(Body);
            if (authenticated)
                fixture.Request.Headers[NoReplayAttempt.ProofHeader] = new string('0', 64);
            Assert.ThrowsException<ProxyErrorException>(() =>
                NoReplayAttempt.BindAuthenticated(fixture.Request, authenticated, fixture.Options));
        }
        Assert.AreEqual(0, server.Requests.Count);
        await using var valid = await WorkerFixture.Create([server], true);
        using var result = await valid.Send();
        Assert.AreEqual(1, server.Requests.Count);
    }

    [TestMethod]
    public async Task SerializedBoundedMetadataIsNotRecoverableAsAFreshLegacyAttempt()
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(500)));
        await using var fixture = await WorkerFixture.Create([server], true);
        Assert.ThrowsException<ProxyErrorException>(() => new RequestDataDtoV1(fixture.Request));
        foreach (string? marker in new[] { NoReplayAttempt.Version, "unknown", null })
        {
            var dto = new RequestDataDtoV1 { AttemptContract = marker, Path = Path, Method = "POST" };
            if (marker is null)
                dto.Headers[NoReplayAttempt.RequestHeader] = Header(Body);
            var roundtrip = RequestDataDtoV1.Deserialize(dto.Serialize())!;
            Assert.ThrowsException<InvalidOperationException>(() => roundtrip.PopulateInto(new RequestData()));
        }
        Assert.AreEqual(0, server.Requests.Count);
        await using var legacy = await WorkerFixture.Create([server], false);
        var restored = new RequestData();
        RequestDataDtoV1.Deserialize(new RequestDataDtoV1(legacy.Request).Serialize())!.PopulateInto(restored);
        restored.setBody(Body);
        restored.defaultTimeout = legacy.Options.Timeout;
        using var response = await legacy.Send(restored);
        Assert.AreEqual(1, server.Requests.Count);
    }

    [TestMethod]
    public async Task OneUseHttp11TransportCannotReplayOnAuthChallengeOrEmptyResponse()
    {
        foreach (int code in new[] { 200, 401 })
        {
            await using var server = new WireServer(_ => Task.FromResult(new WireReply(code, Body: "")));
            await using var fixture = await WorkerFixture.Create([server], true);
            using var result = await fixture.Send();
            Assert.AreEqual(code, (int)result.StatusCode);
            Assert.AreEqual(1, server.Requests.Count);
            var sent = server.Requests.Single();
            Assert.AreEqual("HTTP/1.1", sent.Version);
            Assert.AreEqual("close", sent.Headers["Connection"]);
            Assert.IsFalse(sent.Headers.ContainsKey("Expect"));
            fixture.Request.BackendAttempts = 0;
            await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => fixture.Send());
            Assert.AreEqual(1, server.Requests.Count);
        }
    }

    [TestMethod]
    public async Task CancellationAfterAcceptanceCannotAdvanceToTheNextHost()
    {
        foreach (bool bounded in new[] { false, true })
        {
            var accepted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            await using var first = new WireServer(async _ =>
            {
                accepted.SetResult();
                await Task.Delay(200);
                return new WireReply(500, AllowDisconnect: true);
            });
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            using var cancellation = new CancellationTokenSource();
            await using var fixture = await WorkerFixture.Create(
                [first, second], bounded, cancellation: cancellation.Token);
            var pending = fixture.Send();
            await accepted.Task.WaitAsync(TimeSpan.FromSeconds(3));
            cancellation.Cancel();
            using var result = await pending;
            Assert.AreEqual(bounded ? 1 : 2, first.Requests.Count + second.Requests.Count);
            Assert.AreEqual(bounded ? 408 : 200, (int)result.StatusCode);
        }
    }

    internal static string Header(byte[] body) =>
        $"{NoReplayAttempt.Version}.{new string('a', 32)}.{Convert.ToHexStringLower(SHA256.HashData(body))}";

    internal static void VerifySignature(WireRequest request)
    {
        string value = request.Headers[NoReplayAttempt.RequestHeader];
        Assert.AreEqual(Convert.ToHexStringLower(SHA256.HashData(request.Body)), value.Split('.')[2]);
        string signed = $"{value}\nPOST\n{request.Path}\n{request.Headers["x-LLMModel"]}";
        string expected = Convert.ToHexStringLower(HMACSHA256.HashData(Encoding.UTF8.GetBytes(Key), Encoding.UTF8.GetBytes(signed)));
        Assert.AreEqual(expected, request.Headers[NoReplayAttempt.ProofHeader]);
    }

    private static void SetCircuit(HostConfig config) =>
        typeof(HostConfig).GetField("_circuitBreaker", BindingFlags.NonPublic | BindingFlags.Instance)!
            .SetValue(config, new HealthyCircuit());

    private sealed class HealthyCircuit : ICircuitBreaker
    {
        public string ID { get; set; } = "fixture";
        public void TrackStatus(int code, bool wasFailure, string state) { }
        public Task<bool> CheckFailedStatusAsync(bool nosleep = false) => Task.FromResult(false);
        public void Deregister() { }
        public string GetCircuitBreakerStatusString() => "closed";
    }

    internal sealed class FixtureHost(HostConfig config) : BaseHostHealth(config, NullLogger.Instance)
    {
        public override double SuccessRate() => 1;
        public override void AddCallSuccess(bool success) { }
        public override bool SupportsProbing => false;
    }

    private sealed class Backends(List<BaseHostHealth> hosts) : IEndpointMonitorService
    {
        public List<BaseHostHealth> GetHosts() => hosts;
        public List<BaseHostHealth> GetActiveHosts() => hosts;
        public List<BaseHostHealth> GetSpecificPathHosts() => hosts.Where(h => h.Config.PartialPath != "/").ToList();
        public List<BaseHostHealth> GetCatchAllHosts() => hosts.Where(h => h.Config.PartialPath == "/").ToList();
        public int ActiveHostCount() => hosts.Count;
        public string HostStatus => "healthy";
        public Task<bool> CheckFailedStatusAsync(bool nosleep = false) => Task.FromResult(false);
        public Task WaitForStartupAsync() => Task.CompletedTask;
        public Task Stop() => Task.CompletedTask;
    }

    internal sealed class RecordingQueue : IConcurrentPriQueue<RequestData>
    {
        public readonly TaskCompletionSource Enqueued = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int thrdSafeCount { get; private set; }
        public int MaxQueueLength => 10;
        public bool Enqueue(RequestData item, int priority, int priority2, DateTime timestamp, bool allowOverflow = false)
        {
            thrdSafeCount++;
            Enqueued.TrySetResult();
            return true;
        }
        public bool Requeue(RequestData item, int priority, int priority2, DateTime timestamp) =>
            Enqueue(item, priority, priority2, timestamp);
        public Task<RequestData> DequeueAsync(int preferredPriority) => throw new NotSupportedException();
        public Task StopAsync() => Task.CompletedTask;
        public void StartSignaler(CancellationToken token) { }
        public Task SignalWorker(CancellationToken token) => Task.CompletedTask;
    }

    public class UnusedDependency : DispatchProxy
    {
        protected override object? Invoke(MethodInfo? method, object?[]? args) =>
            throw new AssertFailedException($"Unexpected dependency call: {method?.Name}");
    }

    internal sealed class WorkerFixture : IAsyncDisposable
    {
        internal readonly RecordingQueue Queue = new();
        internal readonly ProxyConfig Options = new();
        internal readonly List<BaseHostHealth> Hosts = [];
        internal RequeueDelayWorker Requeue = null!;
        internal RequestData Request = null!;
        private ProxyWorker _worker = null!;
        private HttpListener _ingress = null!;
        private readonly HttpClient _caller = new(new SocketsHttpHandler { UseProxy = false });
        private Task<HttpResponseMessage> _incoming = null!;
        private SharedIteratorRegistry _registry = null!;

        internal static async Task<WorkerFixture> Create(
            WireServer[] servers, bool bounded, int timeout = 3000, bool shared = true,
            CancellationToken cancellation = default, bool? versioned = null,
            bool legacyHost = false, bool staged = true)
        {
            var f = new WorkerFixture();
            f.Options.Client = new HttpClient(new SocketsHttpHandler { UseProxy = false });
            f.Options.LoadBalanceMode = Constants.Latency;
            f.Options.UseSharedIterators = shared;
            f.Options.IterationMode = IterationModeEnum.SinglePass;
            f.Options.UseProfiles = false;
            f.Options.Timeout = timeout;
            f.Options.TrackWorkers = false;
            typeof(HealthCheckService).GetField("_options", BindingFlags.NonPublic | BindingFlags.Static)!
                .SetValue(null, f.Options);
            f.Options.StripRequestHeaders = ["S7P-KEY"];
            bool route = versioned ?? bounded;
            foreach (var server in servers)
            {
                string routing = route && staged
                    ? $"path={NoReplayAttempt.RoutePrefix};stripprefix=false;retryafter=false;api-key={Key}"
                    : $"retryafter=true;api-key={LegacyKey}";
                var config = new HostConfig($"host={server.Url};mode=apim;api-key-header=Ocp-Apim-Subscription-Key;{routing}");
                SetCircuit(config);
                f.Hosts.Add(new FixtureHost(config));
                if (legacyHost && route && staged)
                {
                    var legacy = new HostConfig($"host={server.Url};mode=apim;api-key-header=Ocp-Apim-Subscription-Key;api-key={LegacyKey}");
                    SetCircuit(legacy);
                    f.Hosts.Add(new FixtureHost(legacy));
                }
            }
            f.Requeue = new RequeueDelayWorker(NullLogger<RequeueDelayWorker>.Instance, f.Queue);
            var notifier = new ConfigChangeNotifier(NullLogger<ConfigChangeNotifier>.Instance);
            f._registry = new SharedIteratorRegistry(NullLogger<SharedIteratorRegistry>.Instance, f.Options, notifier);
            var context = new WorkerContext(
                f.Options, f.Queue, new Backends(f.Hosts),
                DispatchProxy.Create<IUserPriorityService, UnusedDependency>(),
                DispatchProxy.Create<IUserProfileService, UnusedDependency>(), f.Requeue,
                DispatchProxy.Create<IEventClient, UnusedDependency>(), NullLogger<ProxyWorker>.Instance,
                new StreamProcessorFactory(NullLogger<StreamProcessorFactory>.Instance),
                new RequestLifecycleManager(NullLogger<RequestLifecycleManager>.Instance, Microsoft.Extensions.Options.Options.Create(f.Options)),
                new EventDataBuilder(NullLogger<EventDataBuilder>.Instance, Microsoft.Extensions.Options.Options.Create(f.Options)),
                // The public dispatch seam never calls health or app-configuration IO.
                (HealthCheckService)RuntimeHelpers.GetUninitializedObject(typeof(HealthCheckService)),
                notifier, new StreamFlusher(f.Options), sharedIteratorRegistry: f._registry);
            f._worker = new ProxyWorker(0, 2, context, cancellation);
            using var socket = new TcpListener(IPAddress.Loopback, 0);
            socket.Start();
            int port = ((IPEndPoint)socket.LocalEndpoint).Port;
            socket.Stop();
            f._ingress = new HttpListener();
            f._ingress.Prefixes.Add($"http://127.0.0.1:{port}/");
            f._ingress.Start();
            f._incoming = f._caller.PostAsync(
                $"http://127.0.0.1:{port}{(route ? BoundedPath : Path)}", new ByteArrayContent(Body));
            var incoming = await f._ingress.GetContextAsync().WaitAsync(TimeSpan.FromSeconds(5));
            f.Request = new RequestData(incoming, "fixture")
            {
                Guid = Guid.NewGuid(), EnqueueTime = DateTime.UtcNow, DequeueTime = DateTime.UtcNow,
                ExpiresAt = DateTime.UtcNow.AddSeconds(30), defaultTimeout = timeout,
            };
            if (bounded)
            {
                f.Request.Headers[NoReplayAttempt.RequestHeader] = Header(Body);
                NoReplayAttempt.BindAuthenticated(f.Request, true, f.Options);
            }
            return f;
        }

        internal Task<ProxyData> Send(RequestData? request = null) =>
            _worker.ProxyToBackEndAsync(request ?? Request);

        public async ValueTask DisposeAsync()
        {
            Request.SkipDispose = false;
            await Request.DisposeAsync();
            _ingress.Close();
            try { (await _incoming).Dispose(); }
            catch (HttpRequestException) { /* The dispatch harness aborts ingress at cleanup. */ }
            _caller.Dispose();
            Requeue.Dispose();
            _registry.Dispose();
            Options.Client!.Dispose();
        }
    }
}

internal sealed record WireRequest(string Path, string Version, Dictionary<string, string> Headers, byte[] Body, string Method = "POST");
internal sealed record WireReply(int Status, bool Drop = false, bool Requeue = false, bool Ack = true, string? Location = null, string Body = "{}", Dictionary<string, string[]>? Headers = null, bool AllowDisconnect = false);

internal sealed class WireServer : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _stop = new();
    private readonly Func<WireRequest, Task<WireReply>> _reply;
    private readonly Task _pump;
    private readonly ConcurrentBag<Task> _connections = [];
    internal readonly ConcurrentQueue<WireRequest> Requests = new();
    internal string Url { get; }

    internal WireServer(Func<WireRequest, Task<WireReply>> reply)
    {
        _reply = reply;
        _listener.Start();
        Url = $"http://127.0.0.1:{((IPEndPoint)_listener.LocalEndpoint).Port}";
        _pump = Run();
    }

    private async Task Run()
    {
        try
        {
            while (!_stop.IsCancellationRequested)
            {
                var client = await _listener.AcceptTcpClientAsync(_stop.Token);
                _connections.Add(Serve(client));
            }
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
    }

    private async Task Serve(TcpClient client)
    {
        using (client)
        {
            var stream = client.GetStream();
            var bytes = new List<byte>();
            var one = new byte[1];
            while (bytes.Count < 32768)
            {
                if (await stream.ReadAsync(one, _stop.Token) == 0)
                    throw new AssertFailedException("Connection ended before request headers.");
                bytes.Add(one[0]);
                if (bytes.Count >= 4 && bytes.TakeLast(4).SequenceEqual("\r\n\r\n"u8.ToArray()))
                    break;
            }
            var lines = Encoding.ASCII.GetString(bytes.ToArray()).Split("\r\n");
            var start = lines[0].Split(' ');
            var headers = lines.Skip(1).Where(line => line.Contains(':')).ToDictionary(
                line => line[..line.IndexOf(':')], line => line[(line.IndexOf(':') + 1)..].Trim(),
                StringComparer.OrdinalIgnoreCase);
            var body = new byte[int.Parse(headers.GetValueOrDefault("Content-Length", "0"))];
            await stream.ReadExactlyAsync(body, _stop.Token);
            var request = new WireRequest(start[1], start[2], headers, body, start[0]);
            Requests.Enqueue(request);
            var response = await _reply(request);
            if (response.Drop)
                return;
            string ack = response.Ack && headers.TryGetValue(NoReplayAttempt.RequestHeader, out var value)
                ? $"{NoReplayAttempt.AckHeader}: {value[..value.LastIndexOf('.')]}\r\n" : "";
            string extra = response.Requeue ? "S7PREQUEUE: true\r\nretry-after-ms: 1\r\n" : "";
            if (response.Location is not null) extra += $"Location: {response.Location}\r\n";
            if (response.Status == 401) extra += "WWW-Authenticate: Basic realm=\"fixture\"\r\n";
            if (response.Headers is not null)
                foreach (var pair in response.Headers)
                    if (!new[] { "Content-Length", "Content-Type", "Connection", "Transfer-Encoding" }.Contains(pair.Key, StringComparer.OrdinalIgnoreCase))
                        extra += $"{pair.Key}: {string.Join(",", pair.Value)}\r\n";
            var content = Encoding.UTF8.GetBytes(response.Body);
            var head = Encoding.ASCII.GetBytes($"HTTP/1.1 {response.Status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {content.Length}\r\nConnection: close\r\n{ack}{extra}\r\n");
            try
            {
                await stream.WriteAsync(head);
                await stream.WriteAsync(content);
            }
            catch (IOException) when (response.AllowDisconnect)
            {
                // Timeout fixtures deliberately disconnect before the delayed reply.
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        await _pump;
        _listener.Stop();
        await Task.WhenAll(_connections);
        _stop.Dispose();
    }
}
