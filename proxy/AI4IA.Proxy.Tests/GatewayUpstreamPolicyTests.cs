using System.Runtime.CompilerServices;
using System.Text.RegularExpressions;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using SimpleL7Proxy.Auth;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Binds the authored gateway.bicep proxy settings to the vendored parser, host
/// configuration and circuit breaker, so a pin refresh that changes their meaning fails here.
/// </summary>
[TestClass]
[DoNotParallelize] // HostConfig keeps its service provider in static state.
public sealed class GatewayUpstreamPolicyTests
{
    private const string LoopbackGateway = "http://127.0.0.1:9";

    [TestMethod]
    public void AuthoredDisallowedHeadersStripUpstreamCallerControls()
    {
        var match = Regex.Match(GatewaySource(),
            @"name:\s*'DisallowedHeaders'\s+value:\s*string\(\[(?<items>[^\]]*)\]\)");
        Assert.IsTrue(match.Success, "gateway.bicep must author DisallowedHeaders for the proxy");
        var authored = Regex.Matches(match.Groups["items"].Value, "'([^']+)'")
            .Select(item => item.Groups[1].Value).ToArray();
        // Bicep's string([...]) renders a JSON array, which is what the proxy receives.
        string rendered = "[" + string.Join(",", authored.Select(item => $"\"{item}\"")) + "]";
        var configured = ConfigParser.ApplyEnv(new() { ["DisallowedHeaders"] = rendered }, new ProxyConfig());
        foreach (string header in new[] { "S7P-Model-Override", "S7PDEBUGBODY" })
        {
            Assert.IsTrue(configured.DisallowedHeaders.Contains(header, StringComparer.OrdinalIgnoreCase), header);
            // Control: without the authored policy upstream strips nothing.
            Assert.IsFalse(new ProxyConfig().DisallowedHeaders.Contains(header, StringComparer.OrdinalIgnoreCase), header);
        }
    }

    [DataTestMethod]
    [DataRow("Host1")]
    [DataRow("Host2")]
    public void AuthoredHostsDoNotLetOneRetryAfterBlockTheGateway(string name)
    {
        string authored = AuthoredHosts()[name];
        StringAssert.Contains(authored, ";retryafter=false");
        using var services = Services();
        HostConfig.Initialize(NullLogger.Instance, services);
        foreach (bool upstreamDefault in new[] { false, true })
        {
            // The control flips only this flag back to the upstream default.
            var config = new HostConfig(upstreamDefault
                ? authored.Replace(";retryafter=false", ";retryafter=true", StringComparison.Ordinal)
                : authored);
            try
            {
                config.Activate();
                Assert.AreEqual(upstreamDefault, config.UsesRetryAfter);
                using var response = new HttpResponseMessage(System.Net.HttpStatusCode.ServiceUnavailable);
                response.Headers.TryAddWithoutValidation("retry-after-ms", "30000");
                config.TrackStatus(503, true, "fixture", response.Headers);
                // One 5xx carrying APIM's retry-after-ms must not block the only catch-all host.
                Assert.AreEqual(upstreamDefault, config.GetMsToNextRetry() > 0);
            }
            finally
            {
                config.SpinDown();
            }
        }
    }

    private static Dictionary<string, string> AuthoredHosts()
    {
        var matches = Regex.Matches(GatewaySource(), @"name:\s*'(Host[12])'\s+value:\s*'([^']+)'");
        Assert.AreEqual(2, matches.Count);
        return matches.ToDictionary(
            match => match.Groups[1].Value,
            match => match.Groups[2].Value.Replace("${sharedApimGatewayUrl}", LoopbackGateway));
    }

    private static ServiceProvider Services()
    {
        var options = Options.Create(new ProxyConfig());
        var services = new ServiceCollection();
        services.AddSingleton<IOptions<ProxyConfig>>(options);
        services.AddTransient<ICircuitBreaker>(_ => new CircuitBreaker(options, NullLogger<CircuitBreaker>.Instance));
        services.AddSingleton<IBackendTokenProvider, AzureProvider>();
        return services.BuildServiceProvider();
    }

    private static string GatewaySource() => File.ReadAllText(Path.GetFullPath(Path.Combine(
        Path.GetDirectoryName(SourceFile())!, "..", "..", "infra", "modules", "gateway.bicep")));

    private static string SourceFile([CallerFilePath] string path = "") => path;

    // HostConfig resolves its token provider by type name; the authored hosts use API keys only.
    private sealed class AzureProvider : IBackendTokenProvider
    {
        public void AddAudience(string audience) => throw new AssertFailedException("Unexpected audience.");
        public Task<string> OAuth2Token(string? audience = null) => Task.FromResult(string.Empty);
        public void StartTokenRefresh() { }
    }
}
