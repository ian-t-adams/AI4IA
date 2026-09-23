using System.Security.Cryptography;
using System.Text;
using System.Xml.Linq;
using Newtonsoft.Json.Linq;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

// Drives the committed generated catalog and priority fragments against
// loopback regional providers. Retry, throttle and cache decisions come from
// the actual policy expressions; this is not live APIM, quota or cache evidence.
[TestClass]
[DoNotParallelize]
public sealed class ThrottleFailoverApimTests
{
    private const string ApiVersion = "?api-version=2025-04-01-preview";
    private static readonly byte[] Body = Encoding.UTF8.GetBytes(
        """{"messages":[{"role":"user","content":"synthetic fixture"}]}""");
    private static readonly Lazy<(Row Preferred, Row Alternate, Row Single)> Rows = new(FindRows);

    private sealed record Row(string Region, string Deployment)
    {
        internal string Label => Region.ToUpperInvariant();
        internal string ProviderPath => $"/openai/deployments/{Deployment}/chat/completions";
    }

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    [DataTestMethod]
    [DataRow(500, 0)]
    [DataRow(503, 0)]
    [DataRow(429, 30)]
    public async Task TemporaryErrorRetriesTheOtherGlobalStandardRegion(int status, int retryAfter)
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(status, retryAfter)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(preferred, first, second);
        await policy.Run();
        Backends(policy, preferred, alternate);
        Assert.AreEqual(1, first.Requests.Count, Log(policy));
        Assert.AreEqual(1, second.Requests.Count, Log(policy));
        Assert.AreEqual(preferred.ProviderPath, first.Requests.Single().Path);
        Assert.AreEqual(alternate.ProviderPath, second.Requests.Single().Path);
        Assert.AreEqual(200, policy.Context.Response.StatusCode, Log(policy));
        Assert.AreEqual(2, policy.Sends);
        Assert.AreEqual("2", policy.Context.Response.Headers["x-Backend-Attempts"].Single());
    }

    [DataTestMethod]
    [DataRow(500, 0, 10)]
    [DataRow(429, 30, 32)]
    public async Task FailedRegionWakeIsReturnedPersistedAndHonoredByTheNextRequest(
        int status, int retryAfter, int wakeSeconds)
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(status, retryAfter)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(preferred, first, second);
        DateTime before = DateTime.UtcNow;
        await policy.Run();
        DateTime after = DateTime.UtcNow;
        var backends = Backends(policy, preferred, alternate);
        string failed = ThrottleId(backends, preferred);
        var returned = (JObject)policy.Context.Variables["throttleState"];
        AssertWake(returned, failed, before, after, wakeSeconds, "Returned throttleState");
        Assert.IsTrue(policy.Cache.TryGetValue("throttle-" + policy.Context.Api.Id, out var persisted),
            "The throttle state was never persisted. " + Log(policy));
        AssertWake((JObject)persisted, failed, before, after, wakeSeconds, "Persisted throttleState");
        Assert.IsNull(returned["backends"]![ThrottleId(backends, alternate)], "The healthy region was throttled.");

        var next = Policy(preferred, first, second);
        next.Cache = policy.Cache;
        await next.Run();
        Assert.AreEqual(200, next.Context.Response.StatusCode, Log(next));
        Assert.AreEqual(1, next.Sends, Log(next));
        Assert.AreEqual(1, first.Requests.Count, "The next request started on the throttled region.");
        Assert.AreEqual(2, second.Requests.Count);
    }

    [TestMethod]
    public async Task WhenBothRegionsFailEachIsTriedOnceBeforeRequeue()
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(503)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(500)));
        var policy = Policy(preferred, first, second);
        DateTime before = DateTime.UtcNow;
        await policy.Run();
        DateTime after = DateTime.UtcNow;
        var backends = Backends(policy, preferred, alternate);
        Assert.AreEqual(1, first.Requests.Count, Log(policy));
        Assert.AreEqual(1, second.Requests.Count, Log(policy));
        AssertRequeue(policy, attempts: 2, wakeSeconds: 10);
        var state = (JObject)policy.Context.Variables["throttleState"];
        AssertWake(state, ThrottleId(backends, preferred), before, after, 10, "Preferred region");
        AssertWake(state, ThrottleId(backends, alternate), before, after, 10, "Alternate region");
    }

    // Deployment B shares A's regional endpoint, label and affinity but has its
    // own quota, so A's 429 or 5xx must park A there without parking B.
    [DataTestMethod]
    [DataRow(429, 30)]
    [DataRow(500, 0)]
    public async Task ThrottledDeploymentDoesNotParkAnotherDeploymentInItsRegion(int status, int retryAfter)
    {
        var (preferred, alternate, single) = Rows.Value;
        await using var first = new WireServer(request => Task.FromResult(
            request.Path == preferred.ProviderPath ? Reply(status, retryAfter) : Reply(200)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var a = Policy(preferred, first, second);
        await a.Run();
        var aBackends = Backends(a, preferred, alternate);
        Assert.AreEqual(200, a.Context.Response.StatusCode, Log(a));
        Assert.AreEqual(1, second.Requests.Count, Log(a));

        var b = Policy(single, first, second);
        b.Cache = a.Cache;
        await b.Run();
        var bBackends = Backends(b, single);
        Assert.AreEqual(Affinity(aBackends, preferred), Affinity(bBackends, single));
        Assert.AreEqual(200, b.Context.Response.StatusCode, "A's throttle parked deployment B. " + Log(b));
        Assert.AreEqual(1, b.Sends, Log(b));
        Assert.AreEqual(single.ProviderPath, first.Requests.Last().Path);

        var again = Policy(preferred, first, second);
        again.Cache = a.Cache;
        await again.Run();
        Assert.AreEqual(200, again.Context.Response.StatusCode, Log(again));
        Assert.AreEqual(1, again.Sends, Log(again));
        Assert.AreEqual(2, second.Requests.Count, "A's next request did not start on the other region.");
        Assert.AreEqual(2, first.Requests.Count);
        AssertOnlyMark(a, ThrottleId(aBackends, preferred));
        Assert.AreNotEqual(ThrottleId(aBackends, preferred), ThrottleId(bBackends, single));
    }

    // ErrorScenario records a timed-out call; its mark has the same deployment scope.
    [TestMethod]
    public async Task TimedOutDeploymentDoesNotParkAnotherDeploymentInItsRegion()
    {
        var (preferred, alternate, single) = Rows.Value;
        await using var first = new WireServer(async request =>
        {
            if (request.Path != preferred.ProviderPath) return Reply(200);
            await Task.Delay(1200);
            return new WireReply(200, AllowDisconnect: true);
        });
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var a = Policy(preferred, first, second);
        DateTime before = DateTime.UtcNow;
        await a.Run(context =>
        {
            foreach (JObject backend in (JArray)context.Variables["listBackends"]) backend["timeout"] = 1;
        });
        DateTime after = DateTime.UtcNow;
        var aBackends = Backends(a, preferred, alternate);
        Assert.AreEqual(200, a.Context.Response.StatusCode, Log(a));
        Assert.AreEqual(1, first.Requests.Count, Log(a));
        Assert.AreEqual(1, second.Requests.Count, Log(a));
        AssertWake((JObject)a.Cache["throttle-" + a.Context.Api.Id], ThrottleId(aBackends, preferred),
            before, after, 10, "Timed-out deployment");
        AssertOnlyMark(a, ThrottleId(aBackends, preferred));

        var b = Policy(single, first, second);
        b.Cache = a.Cache;
        await b.Run();
        Backends(b, single);
        Assert.AreEqual(200, b.Context.Response.StatusCode, "A's timeout parked deployment B. " + Log(b));
        Assert.AreEqual(1, b.Sends, Log(b));
        Assert.AreEqual(single.ProviderPath, first.Requests.Last().Path);
    }

    // Paired control: the identical failing provider, but a deployment whose
    // generated catalog row has one backend, still requeues after one attempt.
    [DataTestMethod]
    [DataRow(500, 0, 10)]
    [DataRow(429, 30, 32)]
    public async Task SingleRegionDeploymentStillMakesOneAttemptThenRequeues(
        int status, int retryAfter, int wakeSeconds)
    {
        var (_, _, single) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(status, retryAfter)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(single, first, second);
        await policy.Run();
        Backends(policy, single);
        Assert.AreEqual(1, first.Requests.Count, Log(policy));
        Assert.AreEqual(single.ProviderPath, first.Requests.Single().Path);
        Assert.AreEqual(0, second.Requests.Count);
        Assert.AreEqual(1, policy.Sends);
        AssertRequeue(policy, attempts: 1, wakeSeconds);
    }

    // Paired control: the identical two-region fixture that fails over for an
    // ordinary request must stay one attempt for an attempts-v1 selected request.
    [DataTestMethod]
    [DataRow(500, 0)]
    [DataRow(429, 30)]
    public async Task SelectedAttemptsV1RequestMakesExactlyOneAttemptWithoutFailover(int status, int retryAfter)
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(status, retryAfter)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(preferred, first, second, bounded: true);
        await policy.Run();
        Backends(policy, preferred, alternate);
        Assert.IsTrue((bool)policy.Context.Variables["noReplay"]);
        Assert.AreEqual(1, first.Requests.Count, Log(policy));
        Assert.AreEqual(preferred.ProviderPath, first.Requests.Single().Path);
        Assert.AreEqual(0, second.Requests.Count, "A selected request failed over.");
        Assert.AreEqual(1, policy.Sends);
        Assert.AreEqual(status, policy.Context.Response.StatusCode);
        Assert.IsTrue((bool)policy.Context.Variables["attemptClaimed"]);
        Assert.AreEqual(0, (int)policy.Context.Variables["RetryCount"]);
        Assert.IsTrue(policy.Context.Response.Headers.ContainsKey(NoReplayAttempt.AckHeader));
        Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("S7PREQUEUE"));
    }

    // Paired control: on the same fixture, a healthy or malformed-request reply
    // is served by the preferred region without failover or a throttle mark.
    [DataTestMethod]
    [DataRow(200)]
    [DataRow(400)]
    public async Task HealthyOrPermanentReplyNeitherFailsOverNorThrottles(int status)
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(status)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(preferred, first, second);
        await policy.Run();
        Backends(policy, preferred, alternate);
        Assert.AreEqual(status, policy.Context.Response.StatusCode, Log(policy));
        Assert.AreEqual(1, first.Requests.Count);
        Assert.AreEqual(0, second.Requests.Count);
        Assert.AreEqual(1, policy.Sends);
        var state = (JObject)((JObject)policy.Context.Variables["throttleState"])["backends"]!;
        Assert.AreEqual(0, state.Count, state.ToString());
        Assert.IsFalse(policy.Cache.ContainsKey("throttle-" + policy.Context.Api.Id));
    }

    // The persisted-state assertions depend on APIM's by-value cache: an entry
    // is a copy at store time and a lookup returns another copy.
    [TestMethod]
    public async Task HarnessCacheProjectionStoresAndReturnsCopies()
    {
        var (preferred, alternate, _) = Rows.Value;
        await using var first = new WireServer(_ => Task.FromResult(Reply(200)));
        await using var second = new WireServer(_ => Task.FromResult(Reply(200)));
        var policy = Policy(preferred, first, second);
        var original = new JObject
        {
            ["dirty"] = true, ["backends"] = new JObject { ["seed"] = DateTime.UtcNow.AddMinutes(1) },
        };
        policy.Context.Variables["throttleState"] = original;
        // The generated backend fragment's own key and value expressions.
        var store = ApimPolicyHarness.Policies["backend"].Descendants("cache-store-value").First();
        policy.InheritedInbound = new XElement("fragment", new XElement(store), new XElement("cache-lookup-value",
            new XAttribute("key", store.Attribute("key")!.Value), new XAttribute("variable-name", "probe")));
        await policy.Run();
        Assert.AreEqual(200, policy.Context.Response.StatusCode, Log(policy));
        var stored = (JObject)policy.Cache["throttle-" + policy.Context.Api.Id];
        var probe = (JObject)policy.Context.Variables["probe"];
        Assert.IsFalse(ReferenceEquals(stored, original), "The cache aliases the stored variable.");
        Assert.IsFalse(ReferenceEquals(stored, probe), "A lookup aliases the cache entry.");
        original["backends"]!["seed"] = DateTime.MinValue;
        probe["backends"]!["seed"] = DateTime.MinValue;
        Assert.IsTrue(stored["backends"]!.Value<DateTime>("seed") > DateTime.UtcNow, stored.ToString());
    }

    private static string Root()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "infra", "models.json"))) return directory.FullName;
        throw new AssertFailedException("Catalog unavailable.");
    }

    // One OpenAI chat model supplies both shapes: its two GlobalStandard regions
    // fail over to each other, while a DataZoneStandard deployment alone in its
    // zone has a single backend. Backends() re-checks the generated rows.
    private static (Row, Row, Row) FindRows()
    {
        var catalog = JObject.Parse(File.ReadAllText(
            Path.Combine(Root(), "app", "api", "src", "ai4ia_api", "data", "model_catalog.json")));
        foreach (var model in catalog["models"]!.OfType<JObject>())
        {
            if (model.Value<string>("category") != "chat" || model.Value<string>("api") != "chat" ||
                model.Value<string>("format") != "OpenAI" || model["deploymentTarget"] is not null) continue;
            var options = model["options"]!.OfType<JObject>().ToArray();
            var global = options.Where(o => o.Value<string>("sku") == "GlobalStandard").ToArray();
            if (global.Length != 2 || global[0].Value<string>("region") == global[1].Value<string>("region")) continue;
            var single = options.FirstOrDefault(o => o.Value<string>("sku") == "DataZoneStandard" &&
                global.Any(g => g.Value<string>("region") == o.Value<string>("region")) &&
                options.Count(p => p.Value<string>("sku") == o.Value<string>("sku") &&
                    p.Value<string>("dataZone") == o.Value<string>("dataZone")) == 1);
            if (single is null) continue;
            string region = single.Value<string>("region")!;
            return (ToRow(global.Single(o => o.Value<string>("region") == region)),
                ToRow(global.Single(o => o.Value<string>("region") != region)), ToRow(single));
        }
        throw new AssertFailedException("The catalog has no two-region GlobalStandard chat fixture.");

        static Row ToRow(JObject option) =>
            new(option.Value<string>("region")!, option.Value<string>("deploymentName")!);
    }

    private static ApimPolicyHarness Policy(Row requested, WireServer preferred, WireServer alternate, bool bounded = false)
    {
        string path = (bounded ? NoReplayAttempt.RoutePrefix : "") + requested.ProviderPath + ApiVersion;
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase) { ["x-LLMModel"] = requested.Deployment };
        if (bounded)
        {
            string value = NoReplayWorkerTests.Header(Body);
            headers[NoReplayAttempt.RequestHeader] = value;
            headers[NoReplayAttempt.ProofHeader] = Convert.ToHexStringLower(HMACSHA256.HashData(
                Encoding.UTF8.GetBytes(NoReplayWorkerTests.Key),
                Encoding.UTF8.GetBytes($"{value}\nPOST\n{path}\n{requested.Deployment}")));
        }
        var policy = new ApimPolicyHarness(new WireRequest(path, "HTTP/1.1", headers, Body)) { UseCatalog = true };
        var endpoints = new Dictionary<string, string>
        {
            [Rows.Value.Preferred.Region] = preferred.Url, [Rows.Value.Alternate.Region] = alternate.Url,
        };
        var values = policy.Context.NamedValues;
        var source = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "infra", "models.json")));
        foreach (var region in ((JObject)source["regions"]!).Properties())
        {
            values[$"foundry-{region.Name}-endpoint"] = endpoints.GetValueOrDefault(region.Name, "https://unused.invalid");
            values[$"foundry-{region.Name}-services-endpoint"] = "https://unused-services.invalid";
        }
        values["claude-target-endpoint"] = "https://unused-claude.invalid";
        return policy;
    }

    private static WireReply Reply(int status, int retryAfter = 0) => new(status,
        Body: status == 200 ? "{}" : """{"error":{"code":"fixture"}}""",
        Headers: retryAfter == 0 ? null : new(StringComparer.OrdinalIgnoreCase) { ["Retry-After"] = [retryAfter.ToString()] });

    // The actual generated catalog row: requested region first, then failover.
    private static JArray Backends(ApimPolicyHarness policy, params Row[] expected)
    {
        var backends = (JArray)policy.Context.Variables["listBackends"];
        CollectionAssert.AreEqual(expected.Select(r => r.Label).ToArray(),
            backends.Select(b => b.Value<string>("label")).ToArray());
        CollectionAssert.AreEqual(expected.Select(r => r.Deployment).ToArray(),
            backends.Select(b => b.Value<string>("deployment")).ToArray());
        CollectionAssert.AreEqual(Enumerable.Range(1, expected.Length).ToArray(),
            backends.Select(b => b.Value<int>("priorityGroup")).ToArray());
        return backends;
    }

    private static string Affinity(JArray backends, Row row) =>
        backends.OfType<JObject>().Single(b => b.Value<string>("label") == row.Label).Value<string>("affinity")!;

    private static string ThrottleId(JArray backends, Row row) =>
        backends.OfType<JObject>().Single(b => b.Value<string>("label") == row.Label).Value<string>("throttleId")!;

    private static void AssertOnlyMark(ApimPolicyHarness policy, string throttleId)
    {
        var state = (JObject)policy.Cache["throttle-" + policy.Context.Api.Id];
        CollectionAssert.AreEqual(new[] { throttleId },
            ((JObject)state["backends"]!).Properties().Select(p => p.Name).ToArray(), state.ToString());
    }

    // The backend-section S7PREQUEUE set-header is projected onto the request
    // here, so this checks it ran with the requeue decision, not client delivery.
    private static void AssertRequeue(ApimPolicyHarness policy, int attempts, int wakeSeconds)
    {
        var headers = policy.Context.Response.Headers;
        Assert.AreEqual(429, policy.Context.Response.StatusCode, Log(policy));
        Assert.IsTrue((bool)policy.Context.Variables["Return429"], Log(policy));
        Assert.IsTrue((bool)policy.Context.Variables["RequeueAllowed"]);
        Assert.AreEqual("true", policy.Context.Request.Headers["S7PREQUEUE"].Single());
        Assert.AreEqual(attempts.ToString(), headers["x-Backend-Attempts"].Single());
        int delay = int.Parse(headers["retry-after-ms"].Single());
        Assert.IsTrue(delay > (wakeSeconds - 5) * 1000 && delay <= wakeSeconds * 1000, $"retry-after-ms: {delay}");
    }

    private static void AssertWake(JObject state, string id, DateTime before, DateTime after, int seconds, string source)
    {
        var wake = state["backends"]?[id];
        Assert.IsNotNull(wake, $"{source} has no wake time for the failed backend: {state.ToString(Newtonsoft.Json.Formatting.None)}");
        DateTime value = wake.Value<DateTime>();
        Assert.IsTrue(value >= before.AddSeconds(seconds) && value <= after.AddSeconds(seconds),
            $"{source} wake {value:o} is outside [{before.AddSeconds(seconds):o}, {after.AddSeconds(seconds):o}].");
    }

    private static string Log(ApimPolicyHarness policy) => string.Join(" | ",
        ((JArray)policy.Context.Variables["activityLog"]).Select(entry => entry.Value<string>("message")));
}
