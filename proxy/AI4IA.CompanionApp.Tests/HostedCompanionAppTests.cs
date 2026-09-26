using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using CompanionApp.Ai4ia;
using CompanionApp.Components;
using CompanionApp.Components.Shared;
using CompanionApp.Components.Shared.EventHub;
using Microsoft.AspNetCore.Components;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;

namespace AI4IA.CompanionApp.Tests;

/// <summary>
/// Drives the real vendored CompanionApp host to prove the AI4IA hosted-mode
/// boundary: only allow-listed admins get in, only the telemetry pages exist,
/// outbound HTTP is refused before a connection, nothing fabricated is published,
/// and Event Hubs is MI-only.
/// </summary>
[TestClass]
[DoNotParallelize] // Startup configuration is supplied through process environment variables.
public sealed class HostedCompanionAppTests
{
    private const string MonitorSection = "CompanionApp__EventHubMonitor__";
    private const string AdminSection = "CompanionApp__Admin__";
    private const string AdminPrincipal = "11111111-1111-1111-1111-111111111111";
    private const string AdminGroup = "22222222-2222-2222-2222-222222222222";
    private const string OtherPrincipal = "33333333-3333-3333-3333-333333333333";

    internal static readonly string[] AllowedRoutes = ["/", "/explore", "/eventhub", "/insights", "/Error", "/not-found"];

    // Upstream tool routes that must stay absent from the AI4IA build.
    private static readonly string[] ExcludedRoutes =
    [
        "/url-tester", "/stress-test", "/investigator", "/vision", "/chat", "/abort-test", "/tests",
        "/admin", "/admin/configuration", "/admin/deployment", "/test", "/history",
        "/user-preferences", "/incomplete",
    ];

    [TestMethod]
    public void CompiledRoutesAreExactlyTheTelemetryAllowlist()
    {
        var routes = typeof(App).Assembly.GetTypes()
            .SelectMany(type => type.GetCustomAttributes<RouteAttribute>())
            .Select(route => route.Template)
            .ToHashSet(StringComparer.Ordinal);
        CollectionAssert.AreEquivalent(AllowedRoutes, routes.ToArray());
        foreach (string excluded in ExcludedRoutes)
            Assert.IsFalse(routes.Contains(excluded), excluded);
    }

    [TestMethod]
    public async Task RetainedPagesRenderAndUpstreamToolsAreNotFound()
    {
        using var scope = StartupEnvironment.Default();
        await using var factory = new WebApplicationFactory<App>();
        using var client = AdminClient(factory);
        // Control: the same client renders every retained page.
        foreach (string route in new[] { "/", "/explore", "/eventhub", "/insights" })
        {
            using var response = await client.GetAsync(route);
            Assert.AreEqual(HttpStatusCode.OK, response.StatusCode, route);
        }
        foreach (string route in ExcludedRoutes)
        {
            using var response = await client.GetAsync(route);
            Assert.AreEqual(HttpStatusCode.NotFound, response.StatusCode, route);
        }
    }

    [TestMethod]
    public async Task EveryRequestRequiresAnAllowListedEntraPrincipal()
    {
        using var scope = StartupEnvironment.Default().With(AdminSection + "GroupIds", AdminGroup);
        await using var factory = new WebApplicationFactory<App>();
        using var anonymous = factory.CreateClient(new WebApplicationFactoryClientOptions { AllowAutoRedirect = false });
        var cases = new (string Name, Dictionary<string, string> Headers, HttpStatusCode Expected)[]
        {
            ("no platform principal", new(), HttpStatusCode.Forbidden),
            ("non-GUID principal", new() { [HostedGuard.PrincipalIdHeader] = "admin" }, HttpStatusCode.Forbidden),
            ("unlisted principal", new() { [HostedGuard.PrincipalIdHeader] = OtherPrincipal }, HttpStatusCode.Forbidden),
            ("unlisted principal, other group", Principal(OtherPrincipal, "44444444-4444-4444-4444-444444444444"), HttpStatusCode.Forbidden),
            ("group claim without a principal id", Principal(null, AdminGroup), HttpStatusCode.Forbidden),
            ("malformed principal claims", new()
            {
                [HostedGuard.PrincipalIdHeader] = OtherPrincipal, [HostedGuard.PrincipalHeader] = "not-base64!",
            }, HttpStatusCode.Forbidden),
            ("group under another claim type", Principal(OtherPrincipal, AdminGroup, claimType: "roles"), HttpStatusCode.Forbidden),
            // Controls: the same host admits a listed principal and a listed group member.
            ("listed principal", new() { [HostedGuard.PrincipalIdHeader] = AdminPrincipal }, HttpStatusCode.OK),
            ("listed principal, other case", new() { [HostedGuard.PrincipalIdHeader] = AdminPrincipal.ToUpperInvariant() }, HttpStatusCode.OK),
            ("member of a listed group", Principal(OtherPrincipal, AdminGroup), HttpStatusCode.OK),
        };
        foreach (var (name, headers, expected) in cases)
        {
            foreach (string route in new[] { "/", "/eventhub", "/_framework/blazor.web.js", "/url-tester" })
            {
                using var request = new HttpRequestMessage(HttpMethod.Get, route);
                foreach (var (header, value) in headers)
                    request.Headers.TryAddWithoutValidation(header, value);
                using var response = await anonymous.SendAsync(request);
                // An admitted request reaches routing, where an excluded tool is still a 404.
                var effective = expected == HttpStatusCode.OK && route == "/url-tester" ? HttpStatusCode.NotFound : expected;
                Assert.AreEqual(effective, response.StatusCode, $"{name}: {route}");
                if (expected == HttpStatusCode.Forbidden)
                    Assert.AreEqual(0, (await response.Content.ReadAsByteArrayAsync()).Length, $"{name}: {route}");
            }
        }
    }

    [DataTestMethod]
    [DataRow("", "")]
    [DataRow(" , ", "")]
    [DataRow("admins", "")]
    [DataRow("", "not-a-guid")]
    public async Task AnEmptyOrMalformedAdminAllowListRefusesStartup(string groups, string principals)
    {
        using (StartupEnvironment.Default()
            .With(AdminSection + "GroupIds", groups)
            .With(AdminSection + "PrincipalIds", principals))
        {
            await using var refused = new WebApplicationFactory<App>();
            var error = Assert.ThrowsException<InvalidOperationException>(() => refused.CreateClient());
            StringAssert.Contains(error.ToString(), "CompanionApp");
        }
        // Control: a group-only allow-list starts the identical host.
        using var scope = StartupEnvironment.Default()
            .With(AdminSection + "PrincipalIds", null)
            .With(AdminSection + "GroupIds", AdminGroup);
        await using var factory = new WebApplicationFactory<App>();
        using var client = factory.CreateClient();
        using var request = new HttpRequestMessage(HttpMethod.Get, "/");
        foreach (var (header, value) in Principal(OtherPrincipal, AdminGroup))
            request.Headers.TryAddWithoutValidation(header, value);
        using var response = await client.SendAsync(request);
        Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
    }

    [TestMethod]
    public async Task OutboundHttpIsRefusedBeforeAnyConnection()
    {
        using var scope = StartupEnvironment.Default();
        await using var factory = new WebApplicationFactory<App>();
        var hosted = factory.Services.GetRequiredService<HttpClient>();
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var target = $"http://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}/";

        await Assert.ThrowsExceptionAsync<InvalidOperationException>(() => hosted.GetAsync(target));
        Assert.IsFalse(listener.Pending(), "the hosted client opened a connection");

        // Control: an ordinary client against the same listener does connect.
        using var ordinary = new HttpClient { Timeout = TimeSpan.FromMilliseconds(500) };
        var pending = ordinary.GetAsync(target);
        using var accepted = await listener.AcceptTcpClientAsync().WaitAsync(TimeSpan.FromSeconds(5));
        Assert.IsTrue(accepted.Connected);
        await Assert.ThrowsExceptionAsync<TaskCanceledException>(() => pending);
    }

    [TestMethod]
    public async Task NothingFabricatedIsPublishedAtStartup()
    {
        using var scope = StartupEnvironment.Default();
        await using var factory = new WebApplicationFactory<App>();
        using var client = AdminClient(factory);
        var catalog = factory.Services.GetRequiredService<ProxyMetricsCatalog>();
        Assert.AreEqual(default, catalog.LastPublishedUtc);
        // Every metric is still the catalog's unknown marker, so no seeded values exist.
        Assert.IsTrue(catalog.GetActiveGroups().SelectMany(group => group.Metrics).All(metric => metric.Value == "-"));

        // Control: a real publication is observable through the same catalog.
        catalog.Publish([new ParsedEventRecord(string.Empty, new Dictionary<string, string>
        {
            ["Type"] = "S7P-ProxyRequest", ["MID"] = "fixture", ["Status"] = "200", ["Path"] = "/openai/responses",
        })]);
        Assert.AreNotEqual(default, catalog.LastPublishedUtc);
        Assert.IsTrue(catalog.GetActiveGroups().SelectMany(group => group.Metrics).Any(metric => metric.Value != "-"));
    }

    [DataTestMethod]
    [DataRow(MonitorSection + "ConnectionString")]
    [DataRow(MonitorSection + "CheckpointStorage")]
    [DataRow(HostedGuard.ConnectionStringVariable)]
    public async Task SharedAccessSecretsRefuseStartup(string variable)
    {
        using (StartupEnvironment.Default().With(variable, "Endpoint=sb://fixture.invalid/;SharedAccessKeyName=a;SharedAccessKey=b"))
        {
            await using var refused = new WebApplicationFactory<App>();
            var error = Assert.ThrowsException<InvalidOperationException>(() => refused.CreateClient());
            StringAssert.Contains(error.ToString(), "managed identity only");
        }
        // Control: the identical host starts without the secret.
        using var scope = StartupEnvironment.Default();
        await using var factory = new WebApplicationFactory<App>();
        using var client = AdminClient(factory);
        using var response = await client.GetAsync("/");
        Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
    }

    [TestMethod]
    public void GuardReadsTheBoundConfigurationSection()
    {
        var clean = new ConfigurationBuilder().Build();
        HostedGuard.RequireManagedIdentityOnly(clean);
        var configured = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["CompanionApp:EventHubMonitor:ConnectionString"] = "Endpoint=sb://fixture.invalid/",
            })
            .Build();
        Assert.ThrowsException<InvalidOperationException>(() => HostedGuard.RequireManagedIdentityOnly(configured));
    }

    private static HttpClient AdminClient(WebApplicationFactory<App> factory)
    {
        var client = factory.CreateClient(new WebApplicationFactoryClientOptions { AllowAutoRedirect = false });
        client.DefaultRequestHeaders.Add(HostedGuard.PrincipalIdHeader, AdminPrincipal);
        return client;
    }

    // The Container Apps authentication principal: base64 JSON with typed claims.
    private static Dictionary<string, string> Principal(string? principalId, string group, string claimType = "groups")
    {
        var claims = new { auth_typ = "aad", claims = new[] { new { typ = claimType, val = group } } };
        var headers = new Dictionary<string, string>
        {
            [HostedGuard.PrincipalHeader] = Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(claims))),
        };
        if (principalId is not null)
            headers[HostedGuard.PrincipalIdHeader] = principalId;
        return headers;
    }

    /// <summary>Scoped process environment for host startup, restored on dispose.</summary>
    private sealed class StartupEnvironment : IDisposable
    {
        private readonly Dictionary<string, string?> _previous = new(StringComparer.Ordinal);

        internal static StartupEnvironment Default() => new StartupEnvironment()
            // The telemetry reader stays off: these tests never reach Azure.
            .With(MonitorSection + "eventhub_enabled", "false")
            .With(MonitorSection + "LocalFilePath", null)
            .With(MonitorSection + "ConnectionString", null)
            .With(MonitorSection + "CheckpointStorage", null)
            .With(HostedGuard.ConnectionStringVariable, null)
            .With(AdminSection + "PrincipalIds", AdminPrincipal)
            .With(AdminSection + "GroupIds", null)
            .With("CompanionApp__History__DiskPath", Path.Combine(Path.GetTempPath(), "ai4ia-companion-tests", "history"))
            // Keep generated Data Protection keys out of the source tree.
            .With("CompanionApp__DataProtectionKeysPath", Path.Combine(Path.GetTempPath(), "ai4ia-companion-tests", "keys"));

        internal StartupEnvironment With(string name, string? value)
        {
            if (!_previous.ContainsKey(name))
                _previous[name] = Environment.GetEnvironmentVariable(name);
            Environment.SetEnvironmentVariable(name, value);
            return this;
        }

        public void Dispose()
        {
            foreach (var (name, value) in _previous)
                Environment.SetEnvironmentVariable(name, value);
        }
    }
}
