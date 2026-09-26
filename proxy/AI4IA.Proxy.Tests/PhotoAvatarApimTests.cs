using System.Collections;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.RegularExpressions;
using SimpleL7Proxy;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Backend.Iterators;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

// Drives the actual generated infra/policies/photo-avatars.xml through the offline
// APIM projection (expressions compiled with the SDK compiler) against a loopback
// home account. Not Azure's policy engine or live evidence: it proves the policy's
// own refusals, rewrites, identity boundary and single send.
[TestClass]
[DoNotParallelize]
public sealed class PhotoAvatarApimTests
{
    private const string AvatarId = "ai4ia-0123456789abcdef0123";
    private const string ApiVersion = "api-version=2023-12-01-preview";
    private const string Project = "/CustomAvatar/projects/fixture-project_PhotoAvatar";
    private static readonly string Prefix = "/" + ApimPolicyHarness.PhotoAvatarApiPath;
    private static readonly string[] CallerCredentials =
    [
        "Ocp-Apim-Subscription-Key", "api-key", "S7P-KEY", "S7PTTL", "x-S7PPriority",
        "X-AI4IA-User-Id", "X-AI4IA-App-Id", "X-UserProfile", "x-LLMModel",
    ];

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    private sealed record Operation(string Name, string Method, string Template, bool Avatar);

    // The operation inventory APIM matches against, read from the Bicep source.
    private static readonly Operation[] Operations = Regex.Matches(
            ReadRepo("infra", "modules", "gateway.bicep"),
            @"\{ name: '(photo-avatar-[a-z-]+)', method: '([A-Z]+)', path: '([^']+)', avatar: (true|false) \}")
        .Select(m => new Operation(m.Groups[1].Value, m.Groups[2].Value, m.Groups[3].Value, m.Groups[4].Value == "true"))
        .ToArray();

    private static string ReadRepo(params string[] parts) => File.ReadAllText(Path.GetFullPath(
        Path.Combine([Path.GetDirectoryName(SourceFile())!, "..", "..", .. parts])));

    private static string SourceFile([CallerFilePath] string path = "") => path;

    // APIM matches method + URL template before any policy runs; unmatched is a 404 with no policy.
    private static (string Id, Dictionary<string, string> Matched)? MatchOperation(string method, string path)
    {
        string relative = path.Split('?')[0][Prefix.Length..];
        foreach (var operation in Operations.Where(o => o.Method == method))
        {
            var pattern = "^" + Regex.Replace(Regex.Escape(operation.Template), @"\\\{(\w+)}", "(?<$1>[^/]+)") + "$";
            var match = Regex.Match(relative, pattern);
            if (match.Success)
                return (operation.Name, match.Groups.Keys.Where(k => !int.TryParse(k, out _))
                    .ToDictionary(k => k, k => match.Groups[k].Value));
        }
        return null;
    }

    private static WireRequest Caller(string method, string path, string body = "") => new(
        path, "HTTP/1.1",
        CallerCredentials.ToDictionary(name => name, name => "caller-" + name.ToLowerInvariant(),
            StringComparer.OrdinalIgnoreCase)
            .Append(new("Authorization", "Bearer caller-token"))
            .Append(new("x-correlation-id", "corr-fixture"))
            .ToDictionary(p => p.Key, p => p.Value, StringComparer.OrdinalIgnoreCase),
        Encoding.UTF8.GetBytes(body), method);

    private static async Task<(ApimPolicyHarness Policy, List<WireRequest> Sent)> Send(
        WireRequest request, Action<ApimPolicyHarness>? adjust = null, string? operationId = null,
        Dictionary<string, string>? matched = null, WireReply? reply = null)
    {
        var match = MatchOperation(request.Method, request.Path);
        Assert.IsTrue(operationId is not null || match is not null, $"No operation for {request.Method} {request.Path}");
        await using var home = new WireServer(_ => Task.FromResult(reply ?? new WireReply(200, Body: "{\"state\":\"Running\"}")));
        var policy = new ApimPolicyHarness(request, operationId ?? match!.Value.Id, matched ?? match!.Value.Matched, home);
        adjust?.Invoke(policy);
        await policy.Run();
        return (policy, home.Requests.ToList());
    }

    private static string CreateBody(string prompt = "A friendly host.", string extraProperties = "", string extraTop = "") =>
        "{ " + extraTop + "\"properties\": { \"style\": \"Realistic\", " + extraProperties + "\"prompt\": " +
        Newtonsoft.Json.JsonConvert.ToString(prompt) + " } }";

    [TestMethod]
    public void BicepInventoryIsTheSixExactOperationsWithoutAListOrWildcard()
    {
        CollectionAssert.AreEqual(new[]
        {
            "photo-avatar-features GET /features",
            "photo-avatar-project-read GET /project",
            "photo-avatar-project-create PUT /project",
            "photo-avatar-create PUT /photoavatars/{avatarId}",
            "photo-avatar-read GET /photoavatars/{avatarId}",
            "photo-avatar-delete DELETE /photoavatars/{avatarId}",
        }, Operations.Select(o => $"{o.Name} {o.Method} {o.Template}").ToArray());
        Assert.IsNull(MatchOperation("GET", Prefix + "/photoavatars"), "listing must not match any operation");
        Assert.IsNull(MatchOperation("POST", Prefix + "/photoavatars/" + AvatarId));
    }

    [DataTestMethod]
    [DataRow("GET", "/features", "", "GET", "/customavatar/features/?" + ApiVersion, "")]
    [DataRow("GET", "/project", "", "GET", Project + "?" + ApiVersion, "")]
    [DataRow("PUT", "/project", "", "PUT", Project + "?" + ApiVersion,
        "{\"kind\":\"PhotoAvatar\",\"foundryProjectName\":\"fixture-project\"}")]
    [DataRow("PUT", "/photoavatars/" + AvatarId, "create", "PUT", Project + "/photoavatars/" + AvatarId + "?" + ApiVersion,
        "{\"properties\":{\"prompt\":\"A friendly host.\",\"style\":\"Realistic\"}}")]
    [DataRow("GET", "/photoavatars/" + AvatarId, "", "GET", Project + "/photoavatars/" + AvatarId + "?" + ApiVersion, "")]
    [DataRow("DELETE", "/photoavatars/" + AvatarId, "", "DELETE", Project + "/photoavatars/" + AvatarId + "?" + ApiVersion, "")]
    public async Task EachOperationReachesTheHomeAccountOnceWithApimOwnedPathBodyAndIdentity(
        string method, string route, string body, string providerMethod, string providerPath, string providerBody)
    {
        var (policy, sent) = await Send(Caller(method, Prefix + route, body == "create" ? CreateBody() : body));
        Assert.AreEqual(1, sent.Count, Encoding.UTF8.GetString(policy.Context.Response.Body.Bytes));
        Assert.AreEqual(1, policy.Sends);
        var received = sent.Single();
        Assert.AreEqual(providerMethod, received.Method);
        Assert.AreEqual(providerPath, received.Path);
        Assert.AreEqual(providerBody, Encoding.UTF8.GetString(received.Body));
        Assert.AreEqual(200, policy.Context.Response.StatusCode);
        CollectionAssert.AreEqual(new[] { ("https://cognitiveservices.azure.com", (string?)null) }, policy.Identities);
        Assert.AreEqual("Bearer offline-identity", received.Headers["Authorization"]);
        foreach (var name in CallerCredentials)
            Assert.IsFalse(received.Headers.ContainsKey(name), $"{name} reached the provider");
        Assert.AreEqual("corr-fixture", received.Headers["x-correlation-id"]);
    }

    public static IEnumerable<object[]> Refusals() =>
    [
        ["subscription", "PUT"], ["no-subscription", "PUT"], ["api-path", "PUT"], ["foreign-id", "PUT"],
        ["uppercase-id", "PUT"], ["query", "PUT"], ["method-binding", "PUT"], ["matched-parameter", "PUT"],
        ["unknown-operation", "PUT"], ["extra-top-level", "PUT"], ["unknown-property", "PUT"],
        ["enum", "PUT"], ["object-enum", "PUT"], ["prompt-missing", "PUT"], ["prompt-object", "PUT"],
        ["prompt-too-long", "PUT"], ["prompt-whitespace", "PUT"], ["not-json", "PUT"], ["array-body", "PUT"],
        ["oversize", "PUT"], ["body-on-read", "GET"], ["body-on-delete", "DELETE"], ["body-on-project-create", "PUT"],
    ];

    [DataTestMethod]
    [DynamicData(nameof(Refusals), DynamicDataSourceType.Method)]
    public async Task CraftedRequestsAreRefusedBeforeIdentityOrAnySendWithPassingControl(string defect, string method)
    {
        string avatarPath = Prefix + "/photoavatars/" + AvatarId;
        (WireRequest Request, Action<ApimPolicyHarness>? Adjust, string? Operation, Dictionary<string, string>? Matched)
            Build(bool defective)
        {
            string path = defect switch
            {
                "foreign-id" when defective => Prefix + "/photoavatars/sample-foreign-avatar",
                "uppercase-id" when defective => Prefix + "/photoavatars/ai4ia-0123456789ABCDEF0123",
                "query" when defective => avatarPath + "?api-version=2099-01-01",
                "body-on-project-create" => Prefix + "/project",
                _ => avatarPath,
            };
            string body = defect switch
            {
                "body-on-read" or "body-on-delete" => defective ? "{}" : "",
                "body-on-project-create" => defective ? "{\"kind\":\"PhotoAvatar\",\"foundryProjectName\":\"attacker\"}" : "",
                "extra-top-level" when defective => CreateBody(extraTop: "\"description\": \"x\", "),
                "unknown-property" when defective => CreateBody(extraProperties: "\"hair\": \"Long\", "),
                "enum" when defective => CreateBody(extraProperties: "\"gender\": \"Nonbinary\", "),
                // A non-string value must be a clean 400, not an expression fault (APIM on-error).
                "object-enum" when defective => CreateBody(extraProperties: "\"gender\": { \"value\": \"Female\" }, "),
                "prompt-missing" when defective => "{\"properties\":{\"style\":\"Realistic\"}}",
                "prompt-object" when defective => "{\"properties\":{\"prompt\":{\"text\":\"A host.\"}}}",
                "prompt-too-long" => CreateBody(prompt: new string('a', defective ? 1001 : 1000)),
                "prompt-whitespace" when defective => CreateBody(prompt: "   "),
                "not-json" when defective => "not json",
                "array-body" when defective => "[{\"properties\":{\"prompt\":\"x\"}}]",
                "oversize" => CreateBody() + (defective ? new string(' ', 17000) : ""),
                _ => CreateBody(),
            };
            Action<ApimPolicyHarness>? adjust = defective ? defect switch
            {
                "subscription" => p => p.Context.Subscription!.Id = "ai4ia-proxy-models",
                "no-subscription" => p => p.Context.Subscription = null,
                "api-path" => p => p.Context.Api.Path = "openai",
                _ => null,
            } : null;
            var match = MatchOperation(method, path);
            string? operation = defective ? defect switch
            {
                "method-binding" => "photo-avatar-read",
                "unknown-operation" => "photo-avatar-list",
                _ => null,
            } : null;
            Dictionary<string, string>? matched = defective && defect == "matched-parameter"
                ? new() { ["avatarId"] = "ai4ia-ffffffffffffffffffff" } : null;
            if (match is null)
            {
                // A foreign id still matches the template in APIM; bind it as APIM would.
                operation ??= Operations.Single(o => o.Method == method && o.Avatar).Name;
                matched ??= new() { ["avatarId"] = path.Split('?')[0].Split('/').Last() };
            }
            return (Caller(method, path, body), adjust, operation, matched);
        }

        var bad = Build(defective: true);
        var (refused, refusedSent) = await Send(bad.Request, bad.Adjust, bad.Operation, bad.Matched);
        Assert.AreEqual(0, refusedSent.Count, $"{defect} reached the provider");
        Assert.AreEqual(0, refused.Sends);
        Assert.AreEqual(0, refused.Identities.Count, $"{defect} acquired a managed-identity token");
        Assert.AreEqual(400, refused.Context.Response.StatusCode);
        StringAssert.Contains(Encoding.UTF8.GetString(refused.Context.Response.Body.Bytes), "invalid_photo_avatar_request");

        var good = Build(defective: false);
        var (accepted, acceptedSent) = await Send(good.Request, good.Adjust, good.Operation, good.Matched);
        Assert.AreEqual(1, acceptedSent.Count, $"control for {defect}: " +
            Encoding.UTF8.GetString(accepted.Context.Response.Body.Bytes));
        if (defect == "body-on-project-create")
            Assert.AreEqual("{\"kind\":\"PhotoAvatar\",\"foundryProjectName\":\"fixture-project\"}",
                Encoding.UTF8.GetString(acceptedSent.Single().Body));
    }

    [TestMethod]
    public async Task TheProviderReceivesOnlyTheValidatedValuesNeverTheCallersBytes()
    {
        // Duplicate keys are a parser differential: this projection's Newtonsoft
        // keeps the last value, which validation checks, while another parser may
        // keep the first. APIM re-serializes the validated object, so the provider
        // can only ever see the checked value, once.
        string duplicated = "{\"properties\":{\"prompt\":\"A host.\",\"style\":\"Anime\",\"style\":\"Realistic\"}}";
        var (policy, sent) = await Send(Caller("PUT", Prefix + "/photoavatars/" + AvatarId, duplicated));
        Assert.AreEqual(1, sent.Count, Encoding.UTF8.GetString(policy.Context.Response.Body.Bytes));
        string forwarded = Encoding.UTF8.GetString(sent.Single().Body);
        Assert.AreEqual("{\"properties\":{\"prompt\":\"A host.\",\"style\":\"Realistic\"}}", forwarded);
        Assert.IsFalse(forwarded.Contains("Anime", StringComparison.Ordinal));
        // Control: the reverse order leaves an unlisted last value, which is refused.
        string refusedBody = "{\"properties\":{\"prompt\":\"A host.\",\"style\":\"Realistic\",\"style\":\"Anime\"}}";
        var (refused, refusedSent) = await Send(Caller("PUT", Prefix + "/photoavatars/" + AvatarId, refusedBody));
        Assert.AreEqual(0, refusedSent.Count);
        Assert.AreEqual(400, refused.Context.Response.StatusCode);
    }

    [DataTestMethod]
    [DataRow(500, false)]
    [DataRow(503, false)]
    [DataRow(429, true)]
    [DataRow(201, false)]
    public async Task ProviderStatusesPassThroughAfterExactlyOneSendWithoutRequeue(int status, bool requeue)
    {
        var (policy, sent) = await Send(
            Caller("PUT", Prefix + "/photoavatars/" + AvatarId, CreateBody()),
            reply: new WireReply(status, Requeue: requeue));
        Assert.AreEqual(1, sent.Count);
        Assert.AreEqual(1, policy.Sends);
        Assert.AreEqual(status, policy.Context.Response.StatusCode);
        Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("S7PREQUEUE"));
        Assert.AreEqual("corr-fixture", policy.Context.Response.Headers["x-correlation-id"].Single());
    }

    [TestMethod]
    public async Task ATransportFailureIsOneSendAndABoundedGatewayError()
    {
        var (policy, sent) = await Send(
            Caller("PUT", Prefix + "/photoavatars/" + AvatarId, CreateBody()), reply: new WireReply(200, Drop: true));
        Assert.AreEqual(1, sent.Count);
        Assert.AreEqual(1, policy.Sends);
        Assert.AreEqual(502, policy.Context.Response.StatusCode);
        StringAssert.Contains(Encoding.UTF8.GetString(policy.Context.Response.Body.Bytes), "photo_avatar_gateway_error");
    }

    // ---------------- SimpleL7Proxy host configuration ----------------

    private static Dictionary<string, string> AuthoredHosts(string gatewayUrl)
    {
        string source = ReadRepo("infra", "modules", "gateway.bicep");
        var authored = Regex.Matches(source, @"name:\s*'(Host1|Host-photoavatars)'\s+value:\s*'([^']+)'")
            .ToDictionary(m => m.Groups[1].Value, m => m.Groups[2].Value.Replace("${sharedApimGatewayUrl}", gatewayUrl));
        Assert.AreEqual(2, authored.Count);
        Assert.IsTrue(authored.Values.All(value => !value.Contains("${", StringComparison.Ordinal)));
        return authored;
    }

    private static HostCollectionSnapshot Register(Dictionary<string, string> settings)
    {
        var captured = new VersionedHostConfigTests.CapturedHosts();
        var environment = Environment.GetEnvironmentVariables().Cast<DictionaryEntry>()
            .Where(entry => VersionedHostConfigTests.IsHostSetting((string)entry.Key))
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
        return captured.Current;
    }

    private sealed class SnapshotEndpoints(HostCollectionSnapshot snapshot) : IEndpointMonitorService
    {
        public List<BaseHostHealth> GetSpecificPathHosts() => snapshot.SpecificPathHosts;
        public List<BaseHostHealth> GetCatchAllHosts() => snapshot.CatchAllHosts;
        public List<BaseHostHealth> GetHosts() => throw new AssertFailedException("unexpected");
        public List<BaseHostHealth> GetActiveHosts() => throw new AssertFailedException("unexpected");
        public int ActiveHostCount() => throw new AssertFailedException("unexpected");
        public string HostStatus => throw new AssertFailedException("unexpected");
        public int EMSGetBackpressureDelay() => throw new AssertFailedException("unexpected");
        public Task WaitForStartupAsync() => throw new AssertFailedException("unexpected");
        public Task Stop() => throw new AssertFailedException("unexpected");
    }

    [TestMethod]
    public void TheNamedHostLoadsWithoutHost2AndIsTheOnlyCandidateForAvatarPaths()
    {
        const string gateway = "https://apim.invalid";
        var authored = AuthoredHosts(gateway);
        // Host2 (attempts-v1) is absent: the default-off staging state.
        var snapshot = Register(new()
        {
            ["Host1"] = authored["Host1"], ["Host1-api-key"] = NoReplayWorkerTests.LegacyKey,
            ["Host-photoavatars"] = authored["Host-photoavatars"], ["Host-photoavatars-api-key"] = "photo-avatar-key",
            ["APPENDHOSTSFILE"] = "false",
        });
        Assert.AreEqual(2, snapshot.Hosts.Count);
        var avatars = snapshot.SpecificPathHosts.Single();
        Assert.AreEqual(Prefix, avatars.Config.PartialPath);
        Assert.IsFalse(avatars.Config.StripPrefix);
        Assert.AreEqual("photo-avatar-key", avatars.Config.ApiKey);
        Assert.IsInstanceOfType<NonProbeableHostHealth>(avatars);
        var endpoints = new SnapshotEndpoints(snapshot);
        string path = Prefix + "/photoavatars/" + AvatarId;
        var (hosts, modified) = IteratorFactory.CreateSharedHostSnapshot(endpoints, Constants.Latency, path, Constants.AnyPriority);
        Assert.AreSame(avatars, hosts.Single(), "only the scoped host may carry an avatar request");
        Assert.AreEqual(path, modified, "the exact path must reach APIM unstripped");
        // Control: ordinary model traffic still resolves to the catch-all key only.
        var (ordinary, _) = IteratorFactory.CreateSharedHostSnapshot(
            endpoints, Constants.Latency, "/openai/deployments/fixture/chat/completions", Constants.AnyPriority);
        Assert.AreEqual(NoReplayWorkerTests.LegacyKey, ordinary.Single().Config.ApiKey);
    }

    [TestMethod]
    public void ANumberedHostAfterTheConditionalGapWouldSilentlyVanish()
    {
        var authored = AuthoredHosts("https://apim.invalid");
        var numbered = Register(new()
        {
            ["Host1"] = authored["Host1"], ["Host1-api-key"] = NoReplayWorkerTests.LegacyKey,
            ["Host3"] = authored["Host-photoavatars"], ["Host3-api-key"] = "photo-avatar-key",
            ["APPENDHOSTSFILE"] = "false",
        });
        Assert.AreEqual(1, numbered.Hosts.Count, "Host3 behind an absent Host2 is not loaded");
        Assert.AreEqual(0, numbered.SpecificPathHosts.Count);
        // So an avatar request would have fallen through to the catch-all model key.
        var (fallback, _) = IteratorFactory.CreateSharedHostSnapshot(
            new SnapshotEndpoints(numbered), Constants.Latency, Prefix + "/features", Constants.AnyPriority);
        Assert.AreEqual(NoReplayWorkerTests.LegacyKey, fallback.Single().Config.ApiKey);
    }
}
