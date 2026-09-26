using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml.Linq;
using Newtonsoft.Json.Linq;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class VersionedGatewayRoutingTests
{
    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    [DataTestMethod]
    [DataRow(false, false)]
    [DataRow(false, true)]
    [DataRow(true, false)]
    [DataRow(true, true)]
    public async Task OldAndNewProxyApimCombinationsCannotReachLegacyFromVersioned(
        bool newProxy, bool stagedApi)
    {
        int calls = 0;
        await using var provider = new WireServer(_ => Task.FromResult(
            new WireReply(Interlocked.Increment(ref calls) == 1 ? 500 : 200)));
        await using var alternate = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var front = new FrontDoor(stagedApi, provider, alternate);
        await using var apim = new WireServer(front.Handle);
        if (newProxy)
        {
            await using var proxy = await NoReplayWorkerTests.WorkerFixture.Create([apim], true, legacyHost: true);
            if (stagedApi)
            {
                using var response = await proxy.Send();
                Assert.AreEqual(HttpStatusCode.InternalServerError, response.StatusCode);
            }
            else await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => proxy.Send());
        }
        else
        {
            // Old proxy projection: it may retain the selector but cannot sign
            // the reviewed binding. This is not an old binary/runtime claim.
            using var response = await Send(apim, Signed(NoReplayWorkerTests.BoundedPath, proof: false));
            Assert.IsFalse(response.IsSuccessStatusCode);
        }
        Assert.AreEqual(newProxy && stagedApi ? 1 : 0, provider.Requests.Count);
        Assert.AreEqual(0, alternate.Requests.Count);
        int before = provider.Requests.Count;
        calls = 0;
        await using var ordinary = await NoReplayWorkerTests.WorkerFixture.Create([apim], false);
        using var control = await ordinary.Send();
        Assert.AreEqual(HttpStatusCode.OK, control.StatusCode);
        Assert.AreEqual(before + 2, provider.Requests.Count + alternate.Requests.Count,
            "The identical ordinary path must really retry; this need not be a region failover.");
    }

    [DataTestMethod]
    [DataRow("both-stripped")]
    [DataRow("legacy-key")]
    [DataRow("prefix-stripped")]
    [DataRow("missing-selector")]
    [DataRow("missing-proof")]
    [DataRow("wrong-method")]
    [DataRow("signature")]
    public async Task VersionedRouteOrScopedKeyRefusesBeforeAnyPaidSend(string defect)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var front = new FrontDoor(true, provider);
        await using var apim = new WireServer(front.Handle);
        var request = Signed(NoReplayWorkerTests.BoundedPath);
        switch (defect)
        {
            case "both-stripped":
                request.Headers.Remove(NoReplayAttempt.RequestHeader);
                request.Headers.Remove(NoReplayAttempt.ProofHeader);
                break;
            case "legacy-key": request.Headers["Ocp-Apim-Subscription-Key"] = NoReplayWorkerTests.LegacyKey; break;
            case "prefix-stripped": request = request with { Path = NoReplayWorkerTests.Path }; break;
            case "missing-selector": request.Headers.Remove(NoReplayAttempt.RequestHeader); break;
            case "missing-proof": request.Headers.Remove(NoReplayAttempt.ProofHeader); break;
            case "wrong-method": request = request with { Method = "GET" }; break;
            case "signature": request.Headers[NoReplayAttempt.ProofHeader] = new string('0', 64); break;
        }
        using var denied = await Send(apim, request);
        Assert.IsFalse(denied.IsSuccessStatusCode);
        Assert.AreEqual(0, provider.Requests.Count);
        using var accepted = await Send(apim, Signed(NoReplayWorkerTests.BoundedPath));
        Assert.AreEqual(HttpStatusCode.OK, accepted.StatusCode);
        Assert.AreEqual(1, provider.Requests.Count);
    }

    [DataTestMethod]
    [DataRow("/ai4ia-attempts-v1/openai/responses")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/chat/completions")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/embeddings")]
    public async Task ThreeOperationsExecuteActualCatalogRewriteAndMandatoryPolicy(string path)
    {
        byte[] body = Encoding.UTF8.GetBytes(path.EndsWith("responses", StringComparison.Ordinal)
            ? """{"model":"fixture-text","input":[{"role":"user","content":"hello"}],"store":false,"max_output_tokens":20}"""
            : path.EndsWith("embeddings", StringComparison.Ordinal)
            ? """{"input":["hello"]}"""
            : Encoding.UTF8.GetString(NoReplayWorkerTests.Body));
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var front = new FrontDoor(true, provider);
        await using var apim = new WireServer(front.Handle);
        using var response = await Send(apim, Signed(path, body: body));
        Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
        Assert.AreEqual(1, provider.Requests.Count);
        Assert.AreEqual(path[NoReplayAttempt.RoutePrefix.Length..], provider.Requests.Single().Path);
    }

    [DataTestMethod]
    [DataRow("/ai4ia-attempts-v1x/openai/responses")]
    [DataRow("/ai4ia-attempts-v1/openai/responses/extra")]
    [DataRow("/ai4ia-attempts-v1/openai/%72esponses")]
    [DataRow("/ai4ia-attempts-v1/openai//responses")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/chat%2fcompletions")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/chat/completions/")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/chat/completions?api-version=a&api-version=b")]
    [DataRow("/ai4ia-attempts-v1/openai/deployments/fixture-text/chat/completions?subscription-key=legacy")]
    public async Task EncodedPathsAndWildcardCollisionsHaveNoOrdinaryFallback(string path)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var front = new FrontDoor(true, provider);
        // Direct projection retains malformed wire bytes rather than HttpClient
        // normalizing the URL before the policy can exercise the negative case.
        var rejected = await front.Handle(Signed(path));
        Assert.IsTrue(rejected.Status >= 400);
        Assert.AreEqual(0, provider.Requests.Count);
        var accepted = await front.Handle(Signed(NoReplayWorkerTests.BoundedPath));
        Assert.AreEqual(200, accepted.Status);
        Assert.AreEqual(1, provider.Requests.Count);
    }

    // APIM supplies context.Api.Path as "/ai4ia-attempts-v1"; the membership guard trims
    // slashes, then compares exactly. Only the path varies between these rows.
    [DataTestMethod]
    [DataRow("/ai4ia-attempts-v1", true)]
    [DataRow("ai4ia-attempts-v1", true)]
    [DataRow("/openai", false)]
    [DataRow("/ai4ia-attempts-v10", false)]
    [DataRow("/ai4ia-attempts-v1/x", false)]
    [DataRow("", false)]
    [DataRow(null, false)]
    public async Task VersionedApiPathGuardAdmitsApimsLeadingSlashFormAndStaysExact(string? apiPath, bool admitted)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var policy = new ApimPolicyHarness(Signed(NoReplayWorkerTests.BoundedPath), provider);
        policy.Context.Api.Path = apiPath;
        await policy.Run();
        Assert.AreEqual(admitted ? "deployments/fixture-text/chat/completions" : "",
            policy.Context.Variables["attemptsV1Path"]);
        Assert.AreEqual(admitted ? 1 : 0, provider.Requests.Count);
        Assert.AreEqual(admitted ? 1 : 0, policy.Sends);
        Assert.AreEqual(admitted ? 200 : 400, policy.Context.Response.StatusCode);
        if (!admitted)
            Assert.AreEqual("{\"error\":{\"code\":\"invalid_versioned_attempt_route\"}}",
                policy.Context.Response.Body.As<string>());
    }

    [TestMethod]
    public void TheHarnessSuppliesApimsLeadingSlashApiPathForEachModelApi()
    {
        Assert.AreEqual("/ai4ia-attempts-v1",
            new ApimPolicyHarness(Signed(NoReplayWorkerTests.BoundedPath)).Context.Api.Path);
        Assert.AreEqual("/openai", new ApimPolicyHarness(Signed(NoReplayWorkerTests.Path)).Context.Api.Path);
        Assert.AreEqual("/openai", new ApimContext().Api.Path);
    }

    [TestMethod]
    public async Task IsolatedApiCannotInheritAnUncontrolledMeteredSend()
    {
        foreach (bool versioned in new[] { false, true })
        {
            await using var inherited = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var request = Signed(versioned ? NoReplayWorkerTests.BoundedPath : NoReplayWorkerTests.Path);
            if (!versioned)
            {
                request.Headers.Remove(NoReplayAttempt.RequestHeader);
                request.Headers.Remove(NoReplayAttempt.ProofHeader);
            }
            var policy = new ApimPolicyHarness(request, provider)
            {
                InheritedInbound = XElement.Parse($"""
                    <fragment>
                      <set-backend-service base-url="{inherited.Url}" />
                      <rewrite-uri template="/uncontrolled" />
                      <forward-request http-version="1" follow-redirects="false" timeout="1" />
                    </fragment>
                    """),
            };
            await policy.Run();
            Assert.AreEqual(1, provider.Requests.Count);
            Assert.AreEqual(versioned ? 0 : 1, inherited.Requests.Count);
            Assert.AreEqual(versioned ? 1 : 2, policy.Sends);
        }
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task ExactSameHostRouteNeverUsesTheCatchAllKeyWhenBoundedHostIsAbsent(bool shared)
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using (var missing = await NoReplayWorkerTests.WorkerFixture.Create(
            [server], true, shared: shared, staged: false))
        {
            await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => missing.Send());
            Assert.AreEqual(0, server.Requests.Count);
        }
        await using (var selected = await NoReplayWorkerTests.WorkerFixture.Create(
            [server], true, shared: shared, legacyHost: true))
        {
            using var result = await selected.Send();
            Assert.AreEqual(HttpStatusCode.OK, result.StatusCode);
            Assert.AreEqual(NoReplayWorkerTests.Key, server.Requests.Single().Headers["Ocp-Apim-Subscription-Key"]);
            Assert.AreEqual(NoReplayWorkerTests.BoundedPath, server.Requests.Single().Path);
        }
        await using var ordinary = await NoReplayWorkerTests.WorkerFixture.Create([server], false, shared: shared);
        using var control = await ordinary.Send();
        Assert.AreEqual(2, server.Requests.Count);
        Assert.AreEqual(NoReplayWorkerTests.LegacyKey, server.Requests.Last().Headers["Ocp-Apim-Subscription-Key"]);
    }

    [TestMethod]
    public async Task BothMarkersMissingAtProxyOrRecoveredDtoCannotBecomeOrdinary()
    {
        await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var missing = await NoReplayWorkerTests.WorkerFixture.Create([server], false, versioned: true);
        Assert.ThrowsException<ProxyErrorException>(() =>
            NoReplayAttempt.BindAuthenticated(missing.Request, true, missing.Options));
        await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => missing.Send());
        var dto = new SimpleL7Proxy.DTO.RequestDataDtoV1 { Path = NoReplayWorkerTests.BoundedPath, Method = "POST" };
        Assert.ThrowsException<InvalidOperationException>(() => dto.PopulateInto(new RequestData()));
        Assert.AreEqual(0, server.Requests.Count);
        await using var valid = await NoReplayWorkerTests.WorkerFixture.Create([server], true);
        using var control = await valid.Send();
        Assert.AreEqual(1, server.Requests.Count);
    }

    [TestMethod]
    public async Task DuplicateBodyKeysRefuseBeforeClaimWithOrdinaryByteControl()
    {
        byte[] body = Encoding.UTF8.GetBytes(
            """{"messages":[{"role":"user","content":"hello"}],"messages":[{"role":"user","content":"hello"}]}""");
        foreach (bool bounded in new[] { false, true })
        {
            await using var server = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var proxy = await NoReplayWorkerTests.WorkerFixture.Create([server], false, versioned: bounded);
            proxy.Request.setBody(body);
            if (bounded)
            {
                proxy.Request.Headers[NoReplayAttempt.RequestHeader] = NoReplayWorkerTests.Header(body);
                NoReplayAttempt.BindAuthenticated(proxy.Request, true, proxy.Options);
                await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => proxy.Send());
                Assert.IsFalse(proxy.Request.NoReplay!.Claimed);
            }
            else
            {
                using var result = await proxy.Send();
                CollectionAssert.AreEqual(body, server.Requests.Single().Body);
            }
            Assert.AreEqual(bounded ? 0 : 1, server.Requests.Count);
        }
    }

    [TestMethod]
    public async Task ActualBackendPolicyRefusesNonOpenaiProvidersOnVersionedRoutes()
    {
        foreach (string protocol in new[] { "openai", "anthropic", "mai" })
        {
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var policy = new ApimPolicyHarness(Signed(NoReplayWorkerTests.BoundedPath), provider);
            ((JObject)((JArray)policy.Context.Variables["listBackends"])[0])["path"] = protocol;
            await policy.Run();
            Assert.AreEqual(protocol == "openai" ? 1 : 0, provider.Requests.Count);
            Assert.AreEqual(protocol == "openai" ? 200 : 400, policy.Context.Response.StatusCode);
        }
    }

    private static WireRequest Signed(string path, bool proof = true, byte[]? body = null)
    {
        body ??= NoReplayWorkerTests.Body;
        string value = NoReplayWorkerTests.Header(body);
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["Ocp-Apim-Subscription-Key"] = NoReplayWorkerTests.Key,
            ["x-LLMModel"] = "fixture-text", [NoReplayAttempt.RequestHeader] = value,
        };
        if (proof)
            headers[NoReplayAttempt.ProofHeader] = Convert.ToHexStringLower(HMACSHA256.HashData(
                Encoding.UTF8.GetBytes(NoReplayWorkerTests.Key),
                Encoding.UTF8.GetBytes($"{value}\nPOST\n{path}\nfixture-text")));
        return new WireRequest(path, "HTTP/1.1", headers, body);
    }

    private static async Task<HttpResponseMessage> Send(WireServer server, WireRequest request)
    {
        using var client = new HttpClient(new SocketsHttpHandler { UseProxy = false, AllowAutoRedirect = false });
        using var message = new HttpRequestMessage(new HttpMethod(request.Method), server.Url + request.Path);
        message.Content = new ByteArrayContent(request.Body);
        foreach (var header in request.Headers)
            message.Headers.Add(header.Key, header.Value);
        return await client.SendAsync(message);
    }

    // APIM frontend routing/subscription-scope projection, not an Azure emulator.
    // The independent compiled-ARM tests bind these three paths and key scopes
    // to the resources the source actually deploys. Policy expressions are real.
    private sealed class FrontDoor(bool staged, params WireServer[] providers)
    {
        internal async Task<WireReply> Handle(WireRequest request)
        {
            string path = request.Path.Split('?')[0];
            bool versioned = Regex.IsMatch(path,
                @"\A/ai4ia-attempts-v1/openai/(responses|deployments/[A-Za-z0-9_.-]+/(chat/completions|embeddings))\z");
            bool legacy = path.StartsWith("/openai/", StringComparison.Ordinal);
            if ((!versioned && !legacy) || (versioned && (!staged || request.Method != "POST")))
                return new WireReply(404, Ack: false, AllowDisconnect: true);
            string key = versioned ? NoReplayWorkerTests.Key : NoReplayWorkerTests.LegacyKey;
            if (request.Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key") != key)
                return new WireReply(401, Ack: false, AllowDisconnect: true);
            var policy = new ApimPolicyHarness(request, providers);
            await policy.Run();
            return new WireReply(policy.Context.Response.StatusCode, Ack: false,
                Body: policy.Context.Response.Body.As<string>(), Headers: policy.Context.Response.Headers,
                AllowDisconnect: true);
        }
    }
}
