using System.Collections;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using SimpleL7Proxy;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Config;
using SimpleL7Proxy.Events;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class VersionedHostConfigTests
{
    [DataTestMethod]
    [DataRow(false, false)]
    [DataRow(true, false)]
    [DataRow(true, true)]
    public async Task ActualHostConfigurationDisablesOnlyTheStagedProbe(bool staged, bool omitSentinel)
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
        string source = File.ReadAllText(Path.GetFullPath(Path.Combine(
            Path.GetDirectoryName(SourceFile())!, "..", "..", "infra", "modules", "gateway.bicep")));
        var matches = Regex.Matches(source, @"name:\s*'(Host[12])'\s+value:\s*'([^']+)'");
        Assert.AreEqual(2, matches.Count);
        var authored = matches.ToDictionary(
            match => match.Groups[1].Value,
            match => match.Groups[2].Value.Replace("${sharedApimGatewayUrl}", server.Url));
        Assert.IsTrue(authored.Values.All(value => !value.Contains("${", StringComparison.Ordinal)));
        var settings = new Dictionary<string, string>
        {
            ["Host1"] = authored["Host1"],
            ["Host1-api-key"] = NoReplayWorkerTests.LegacyKey,
            ["APPENDHOSTSFILE"] = "false",
        };
        if (staged)
        {
            settings["Host2"] = omitSentinel
                ? authored["Host2"].Replace(";probe=/;", ";", StringComparison.Ordinal)
                : authored["Host2"];
            settings["Host2-api-key"] = NoReplayWorkerTests.Key;
        }
        Assert.IsFalse(settings.ContainsKey("Probe_path2"));
        var captured = new CapturedHosts();
        var environment = Environment.GetEnvironmentVariables().Cast<DictionaryEntry>()
            .Where(entry => IsHostSetting((string)entry.Key))
            .ToDictionary(entry => (string)entry.Key, entry => (string?)entry.Value);
        try
        {
            foreach (var key in environment.Keys)
                Environment.SetEnvironmentVariable(key, null);
            ConfigFactory.RegisterBackends(new ProxyConfig(), appConfigSettings: settings, hostCollection: captured);
        }
        finally
        {
            foreach (var pair in environment)
                Environment.SetEnvironmentVariable(pair.Key, pair.Value);
        }

        var snapshot = captured.Current;
        Assert.AreEqual(staged ? 2 : 1, snapshot.Hosts.Count);
        Assert.AreEqual(1, snapshot.CatchAllHosts.Count);
        var ordinary = snapshot.CatchAllHosts.Single();
        Assert.IsInstanceOfType<ProbeableHostHealth>(ordinary);
        Assert.IsTrue(ordinary.SupportsProbing);
        Assert.AreEqual("/openai/status", ordinary.Config.ProbePath);
        Assert.AreEqual(NoReplayWorkerTests.LegacyKey, ordinary.Config.ApiKey);
        Assert.AreEqual(staged ? 1 : 0, snapshot.SpecificPathHosts.Count);
        using var http = new HttpClient(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false })
        {
            Timeout = TimeSpan.FromSeconds(5),
        };
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var options = Options.Create(new ProxyConfig { Client = http });
        using var monitor = new EndpointMonitorService(
            options, DispatchProxy.Create<ICircuitBreaker, NoReplayWorkerTests.UnusedDependency>(), captured,
            DispatchProxy.Create<IHostApplicationLifetime, NoReplayWorkerTests.UnusedDependency>(),
            DispatchProxy.Create<IEventClient, NoReplayWorkerTests.UnusedDependency>(), cancellation,
            NullLogger<EndpointMonitorService>.Instance,
            new ReadinessRegistry(options, NullLogger<ReadinessRegistry>.Instance));
        var probe = typeof(EndpointMonitorService)
            .GetMethod("GetHostStatus", BindingFlags.Instance | BindingFlags.NonPublic)!
            .CreateDelegate<Func<BaseHostHealth, HttpClient, Task<bool>>>(monitor);
        foreach (var host in snapshot.Hosts)
            Assert.IsTrue(await probe(host, http));
        Assert.AreEqual(staged && omitSentinel ? 2 : 1, server.Requests.Count);
        Assert.IsTrue(server.Requests.All(request => request.Method == "GET"));
        Assert.AreEqual("/openai/status", server.Requests.Single(
            request => request.Headers["Ocp-Apim-Subscription-Key"] == NoReplayWorkerTests.LegacyKey).Path);
        Assert.AreEqual(staged && omitSentinel ? 1 : 0, server.Requests.Count(
            request => request.Headers["Ocp-Apim-Subscription-Key"] == NoReplayWorkerTests.Key));
        if (staged)
        {
            var bounded = snapshot.SpecificPathHosts.Single();
            Assert.AreEqual(ordinary.Host, bounded.Host);
            Assert.AreEqual("/ai4ia-attempts-v1", bounded.Config.PartialPath);
            Assert.IsFalse(bounded.Config.StripPrefix);
            Assert.AreEqual(NoReplayWorkerTests.Key, bounded.Config.ApiKey);
            Assert.AreEqual(omitSentinel, bounded.SupportsProbing);
            Assert.AreEqual(omitSentinel ? "echo/resource?param1=sample" : "/", bounded.Config.ProbePath);
            if (omitSentinel) Assert.IsInstanceOfType<ProbeableHostHealth>(bounded);
            else Assert.IsInstanceOfType<NonProbeableHostHealth>(bounded);
        }
    }

    private static string SourceFile([CallerFilePath] string path = "") => path;

    internal static bool IsHostSetting(string key) =>
        key.StartsWith("Host", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Probe", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("IP", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Api_Key", StringComparison.OrdinalIgnoreCase) ||
        key.Equals("APPENDHOSTSFILE", StringComparison.OrdinalIgnoreCase);

    // Capture the production loader's HostConfigs and use real categorization,
    // without activating circuits or background services. The test invokes the
    // actual probe method separately against loopback with synthetic keys.
    internal sealed class CapturedHosts : IHostHealthCollection
    {
        private readonly List<HostConfig> _configs = [];
        public HostCollectionSnapshot Current { get; private set; } = HostCollectionSnapshot.Empty;
        public void StageHost(HostConfig config) => _configs.Add(config);
        public void Activate() => Current = HostCollectionSnapshot.Build(_configs, NullLogger.Instance);
        public void ReplaceConfiguration(IEnumerable<HostConfig> configs, IEnumerable<PathRouteDefinition> routes) =>
            Current = HostCollectionSnapshot.Build(configs, routes, NullLogger.Instance);
        public void LoadFromConfig(IEnumerable<HostConfig> configs) => throw new AssertFailedException("Unexpected reload.");
        public BaseHostHealth AddHost(HostConfig config) => throw new AssertFailedException("Unexpected host add.");
        public bool RemoveHost(Guid id) => throw new AssertFailedException("Unexpected host removal.");
        public bool UpdateHost(Guid id, Action<HostConfig> mutate) => throw new AssertFailedException("Unexpected host update.");
    }
}
