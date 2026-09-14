using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Xml.Linq;
using Newtonsoft.Json.Linq;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class NoReplayApimTests
{
    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    [DataTestMethod]
    [DataRow(500, false)]
    [DataRow(429, false)]
    [DataRow(200, true)]
    public async Task RealProxyThroughActualPolicyExpressionsBoundsProviderSends(int status, bool empty)
    {
        foreach (bool bounded in new[] { false, true })
        {
            int calls = 0;
            await using var first = new WireServer(_ => Task.FromResult(
                Interlocked.Increment(ref calls) == 1
                    ? new WireReply(status, Body: empty ? "" : "{}") : new WireReply(200)));
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            ApimPolicyHarness? observed = null;
            await using var apim = new WireServer(async request =>
            {
                observed = new ApimPolicyHarness(request, first, second);
                await observed.Run();
                var response = observed.Context.Response;
                return new WireReply(response.StatusCode, Ack: false,
                    Body: response.Body.As<string>(), Headers: response.Headers);
            });
            await using var proxy = await NoReplayWorkerTests.WorkerFixture.Create([apim], bounded);
            using var result = await proxy.Send();
            Assert.AreEqual(bounded ? status : 200, (int)result.StatusCode,
                $"bounded={bounded}; counts={first.Requests.Count}/{second.Requests.Count}; " +
                observed!.Context.Variables["activityLog"]);
            Assert.AreEqual(bounded ? 1 : 2, first.Requests.Count + second.Requests.Count);
            Assert.AreEqual(1, apim.Requests.Count);
            Assert.AreEqual(bounded ? 1 : 2, observed!.Sends);
            foreach (var sent in first.Requests.Concat(second.Requests))
            {
                Assert.IsFalse(sent.Headers.ContainsKey(NoReplayAttempt.RequestHeader));
                Assert.IsFalse(sent.Headers.ContainsKey(NoReplayAttempt.ProofHeader));
                Assert.IsFalse(sent.Headers.ContainsKey("Ocp-Apim-Subscription-Key"));
            }
        }
    }

    [TestMethod]
    public async Task TimeoutPolicyErrorCannotRequeueOrRetryAnAlreadyAcceptedRequest()
    {
        foreach (bool bounded in new[] { false, true })
        {
            await using var first = new WireServer(async _ =>
            {
                await Task.Delay(1200);
                return new WireReply(200, AllowDisconnect: true);
            });
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var policy = new ApimPolicyHarness(Request(bounded), first, second);
            await policy.Run();
            Assert.AreEqual(bounded ? 1 : 2, first.Requests.Count + second.Requests.Count);
            Assert.AreEqual(bounded ? 502 : 200, policy.Context.Response.StatusCode);
            Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("S7PREQUEUE"));
            if (bounded)
                Assert.IsTrue(policy.Context.Response.Headers.ContainsKey(NoReplayAttempt.AckHeader));
        }
    }

    [DataTestMethod]
    [DataRow("missing-proof")]
    [DataRow("missing-mode")]
    [DataRow("wrong-key")]
    [DataRow("no-subscription")]
    [DataRow("version")]
    [DataRow("body")]
    [DataRow("model")]
    [DataRow("path")]
    [DataRow("query")]
    [DataRow("nonce")]
    [DataRow("duplicate")]
    [DataRow("unknown-header")]
    public async Task InvalidAuthenticatedMetadataRefusesBeforeAnyProviderSend(string defect)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var request = Request(true);
        var policy = new ApimPolicyHarness(request, provider);
        var headers = policy.Context.Request.Headers;
        switch (defect)
        {
            case "missing-proof": headers.Remove(NoReplayAttempt.ProofHeader); break;
            case "missing-mode": headers.Remove(NoReplayAttempt.RequestHeader); break;
            case "wrong-key": policy.Context.Subscription!.PrimaryKey = "wrong-key"; break;
            case "no-subscription": policy.Context.Subscription = null; break;
            case "version": headers[NoReplayAttempt.RequestHeader] = ["unknown.1"]; break;
            case "body": policy.Context.Request.Body = new ApimBody("{}"u8.ToArray()); break;
            case "model": headers["x-LLMModel"] = ["changed"]; break;
            case "path": policy.Context.Request.OriginalUrl = new ApimUrl("/openai/responses"); break;
            case "query": policy.Context.Request.OriginalUrl = new ApimUrl(NoReplayWorkerTests.Path + "&changed=true"); break;
            case "nonce": headers[NoReplayAttempt.RequestHeader] = [headers[NoReplayAttempt.RequestHeader][0].Replace(new string('a', 32), new string('b', 32))]; break;
            case "duplicate": headers[NoReplayAttempt.RequestHeader] = [headers[NoReplayAttempt.RequestHeader][0], headers[NoReplayAttempt.RequestHeader][0]]; break;
            case "unknown-header": headers["x-ai4ia-attempt-future"] = ["true"]; break;
        }
        await policy.Run();
        Assert.AreEqual(400, policy.Context.Response.StatusCode);
        Assert.AreEqual(0, provider.Requests.Count);
        var valid = new ApimPolicyHarness(Request(true), provider);
        await valid.Run();
        Assert.AreEqual(200, valid.Context.Response.StatusCode);
        Assert.AreEqual(1, provider.Requests.Count);
    }

    [TestMethod]
    public async Task PredispatchClaimFencesActualSendEvenWhenRetryCountersAreReset()
    {
        foreach (bool claimed in new[] { false, true })
        {
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var policy = new ApimPolicyHarness(Request(true), provider);
            await policy.Run(context =>
            {
                context.Variables["RetryCount"] = 99;
                context.Variables["backendCallCounter"] = 0;
                context.Variables["attemptClaimed"] = claimed;
            });
            Assert.AreEqual(claimed ? 0 : 1, provider.Requests.Count);
            Assert.AreEqual(claimed ? 400 : 200, policy.Context.Response.StatusCode);
        }
    }

    [DataTestMethod]
    [DataRow(false, "off")]
    [DataRow(false, "low")]
    [DataRow(false, "medium")]
    [DataRow(false, "high")]
    [DataRow(true, "off")]
    [DataRow(true, "low")]
    [DataRow(true, "medium")]
    [DataRow(true, "high")]
    public async Task EveryForwardBranchUsesTheClaimAndNonredirectingHttp11(bool buffer, string limit)
    {
        await using var destination = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(307, Location: destination.Url)));
        var policy = new ApimPolicyHarness(Request(true), provider);
        foreach (JObject backend in (JArray)policy.Context.Variables["listBackends"])
        {
            backend["bufferResponse"] = buffer;
            backend["limitConcurrency"] = limit;
        }
        await policy.Run();
        Assert.AreEqual(1, provider.Requests.Count);
        Assert.AreEqual(0, destination.Requests.Count);
        Assert.AreEqual(307, policy.Context.Response.StatusCode);
        Assert.IsTrue((bool)policy.Context.Variables["attemptClaimed"]);
        foreach (var forward in ApimPolicyHarness.Policies["backend"].Descendants("forward-request"))
        {
            Assert.AreEqual("false", forward.Attribute("follow-redirects")!.Value);
            Assert.AreEqual(false, policy.Eval(forward.Attribute("buffer-request-body")!.Value));
        }
    }

    [TestMethod]
    public async Task ConcurrencyDenialDoesNotBecomePermissionToTryAnotherBackend()
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var policy = new ApimPolicyHarness(Request(true), provider) { Limit = 1 };
        ((JObject)((JArray)policy.Context.Variables["listBackends"])[0])["limitConcurrency"] = "low";
        await policy.Run();
        Assert.AreEqual(0, provider.Requests.Count);
        Assert.AreEqual(502, policy.Context.Response.StatusCode);
        Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("S7PREQUEUE"));
        var control = new ApimPolicyHarness(Request(true), provider);
        await control.Run();
        Assert.AreEqual(1, provider.Requests.Count);
    }

    [TestMethod]
    public async Task CounterfactualUnenforcedMarkersCanPayTwiceBeforeAckFailure()
    {
        int calls = 0;
        await using var provider = new WireServer(_ => Task.FromResult(
            new WireReply(Interlocked.Increment(ref calls) == 1 ? 500 : 200)));
        await using var alternate = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var apim = new WireServer(async request =>
        {
            // Project an intermediary stripping both fields, or an old policy's
            // lack of membership enforcement, into the unchanged ordinary route.
            var headers = request.Headers.Where(p => !NoReplayAttempt.IsInternalHeader(p.Key))
                .ToDictionary(p => p.Key, p => p.Value, StringComparer.OrdinalIgnoreCase);
            var ordinary = new ApimPolicyHarness(request with { Headers = headers }, provider, alternate);
            await ordinary.Run();
            return new WireReply(ordinary.Context.Response.StatusCode, Ack: false, AllowDisconnect: true);
        });
        await using var proxy = await NoReplayWorkerTests.WorkerFixture.Create([apim], true);
        await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => proxy.Send());
        Assert.AreEqual(1, apim.Requests.Count);
        Assert.AreEqual(2, provider.Requests.Count + alternate.Requests.Count,
            "Missing ACK is too late to prove no paid replay.");
    }

    [TestMethod]
    public async Task NewHttpOperationReusingNonceAfterLostReplyIsNotGloballyDeduplicated()
    {
        int operations = 0;
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var apim = new WireServer(async request =>
        {
            var policy = new ApimPolicyHarness(request, provider);
            await policy.Run();
            return new WireReply(policy.Context.Response.StatusCode, Ack: false,
                Drop: Interlocked.Increment(ref operations) == 1, Headers: policy.Context.Response.Headers);
        });
        await using (var first = await NoReplayWorkerTests.WorkerFixture.Create([apim], true))
        {
            using var result = await first.Send();
            Assert.AreEqual(502, (int)result.StatusCode);
        }
        await using (var second = await NoReplayWorkerTests.WorkerFixture.Create([apim], true))
        {
            using var result = await second.Send();
            Assert.AreEqual(200, (int)result.StatusCode);
        }
        Assert.AreEqual(2, provider.Requests.Count);
        var sent = apim.Requests.ToArray();
        Assert.AreEqual(sent[0].Headers[NoReplayAttempt.RequestHeader], sent[1].Headers[NoReplayAttempt.RequestHeader]);
        CollectionAssert.AreEqual(sent[0].Body, sent[1].Body);
    }

    private static WireRequest Request(bool bounded)
    {
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["x-LLMModel"] = "fixture-text",
            ["Ocp-Apim-Subscription-Key"] = NoReplayWorkerTests.Key,
        };
        if (bounded)
        {
            string value = NoReplayWorkerTests.Header(NoReplayWorkerTests.Body);
            string signed = $"{value}\nPOST\n{NoReplayWorkerTests.Path}\nfixture-text";
            headers[NoReplayAttempt.RequestHeader] = value;
            headers[NoReplayAttempt.ProofHeader] = Convert.ToHexStringLower(HMACSHA256.HashData(
                Encoding.UTF8.GetBytes(NoReplayWorkerTests.Key), Encoding.UTF8.GetBytes(signed)));
        }
        return new WireRequest(NoReplayWorkerTests.Path, "HTTP/1.1", headers, NoReplayWorkerTests.Body);
    }
}
