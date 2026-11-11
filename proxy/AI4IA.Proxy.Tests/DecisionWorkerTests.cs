using System.Net;
using SimpleL7Proxy.DTO;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class DecisionWorkerTests
{
    private const string Path = "/deployments/fixture-decision/systemone";
    private static readonly byte[] Body = """{"state":"synthetic","questions":{}}"""u8.ToArray();

    [DataTestMethod]
    [DataRow(200, false)]
    [DataRow(200, true)]
    [DataRow(429, false)]
    [DataRow(429, true)]
    [DataRow(500, false)]
    [DataRow(503, false)]
    [DataRow(307, false)]
    [DataRow(401, false)]
    public async Task DecisionStopsAfterOneHostWhileOrdinaryWorkCanFailOver(int status, bool requeue)
    {
        foreach (bool decision in new[] { true, false })
        {
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var first = new WireServer(_ => Task.FromResult(new WireReply(
                status, Requeue: requeue, Location: second.Url + Path,
                Body: "private-body-canary", Headers: new() { ["X-Private"] = ["private-header-canary"] })));
            await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(
                first, true, additionalBackends: [second]);
            using var response = await gateway.Post(
                decision ? Path : NoReplayWorkerTests.Path, Body, new());
            bool terminal = decision || status is 200 or 401;
            Assert.AreEqual(terminal ? status : 200, (int)response.StatusCode);
            Assert.AreEqual(terminal ? 1 : 2, first.Requests.Count + second.Requests.Count);
            Assert.AreEqual(0, gateway.Queue.Requeues);
            if (decision && status != 200)
            {
                Assert.IsFalse((await response.Content.ReadAsStringAsync()).Contains("private-"));
                Assert.IsFalse(response.Headers.Contains("X-Private"));
                Assert.IsFalse(response.Headers.Contains("Location"));
                Assert.IsFalse(response.Headers.Contains("BackendHost"));
            }
        }
    }

    [DataTestMethod]
    [DataRow("timeout")]
    [DataRow("lost-response")]
    public async Task DecisionTimeoutOrLostResponseNeverSendsToAnotherHost(string failure)
    {
        foreach (bool decision in new[] { true, false })
        {
            await using var first = new WireServer(async _ =>
            {
                if (failure == "timeout") await Task.Delay(500);
                return new WireReply(200, Drop: failure == "lost-response", AllowDisconnect: true);
            });
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var fixture = await NoReplayWorkerTests.WorkerFixture.Create(
                [first, second], false, timeout: 100, path: decision ? Path : NoReplayWorkerTests.Path, body: Body);
            using var response = await fixture.Send();
            Assert.AreEqual(decision ? failure == "timeout" ? 408 : 502 : 200, (int)response.StatusCode);
            Assert.AreEqual(1, first.Requests.Count);
            Assert.AreEqual(decision ? 0 : 1, second.Requests.Count);
            if (decision)
                await Assert.ThrowsExceptionAsync<ProxyErrorException>(() => fixture.Send());
        }
    }

    [TestMethod]
    public async Task RetryDateIsNormalizedButDuplicateOrUnboundedDatesAreOmitted()
    {
        var future = DateTimeOffset.UtcNow.AddSeconds(60).ToString("r");
        foreach (var values in new[]
        {
            new[] { future }, new[] { "17", "18" },
            new[] { DateTimeOffset.UtcNow.AddDays(2).ToString("r") },
            new[] { DateTimeOffset.UtcNow.AddDays(-1).ToString("r") }
        })
        {
            await using var backend = new WireServer(_ => Task.FromResult(new WireReply(
                429, Headers: new() { ["Retry-After"] = values })));
            await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(backend, true);
            using var response = await gateway.Post(Path, Body, new());
            if (values[0] == future)
            {
                Assert.IsTrue(int.TryParse(response.Headers.GetValues("Retry-After").Single(), out var delay));
                Assert.IsTrue(delay is > 0 and <= 60);
            }
            else Assert.IsFalse(response.Headers.Contains("Retry-After"));
            Assert.AreEqual(1, backend.Requests.Count);
        }
    }

    [TestMethod]
    public async Task AnOpenHostCanBeSkippedBeforeTheSingleDecisionSend()
    {
        await using var first = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
        int hosts = 0;
        await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(
            first, true, additionalBackends: [second],
            circuit: () => Interlocked.Increment(ref hosts) == 1
                ? new NoReplayWorkerTests.OpenCircuit() : new NoReplayWorkerTests.HealthyCircuit());
        using var response = await gateway.Post("/openai" + Path + "?api-version=fixture", Body, new());
        Assert.AreEqual(HttpStatusCode.OK, response.StatusCode);
        Assert.AreEqual(0, first.Requests.Count);
        Assert.AreEqual(1, second.Requests.Count);
        Assert.AreEqual(0, gateway.Queue.Requeues);
    }

    [DataTestMethod]
    [DataRow(null, null)]
    [DataRow("17", "17")]
    [DataRow("0", "0")]
    [DataRow("86400", "86400")]
    [DataRow("-1", null)]
    [DataRow("86401", null)]
    [DataRow("999999999999999999999999999999999999", null)]
    [DataRow("NaN", null)]
    [DataRow("1.5", null)]
    [DataRow("private-header-canary", null)]
    [DataRow("17, 18", null)]
    public async Task Final429RetainsOnlyBoundedRetryGuidance(string? value, string? expected)
    {
        var headers = new Dictionary<string, string[]> { ["X-Private"] = ["private-header-canary"] };
        if (value is not null) headers["Retry-After"] = [value];
        await using var backend = new WireServer(_ => Task.FromResult(new WireReply(
            429, Body: "private-body-canary", Headers: headers)));
        await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(backend, true);
        using var response = await gateway.Post(Path, Body, new());
        Assert.AreEqual(HttpStatusCode.TooManyRequests, response.StatusCode);
        Assert.AreEqual(expected, response.Headers.TryGetValues("Retry-After", out var values) ? values.Single() : null);
        Assert.IsFalse((await response.Content.ReadAsStringAsync()).Contains("private-"));
        Assert.IsFalse(response.Headers.Contains("X-Private"));
        Assert.AreEqual(1, backend.Requests.Count);
        Assert.AreEqual(0, gateway.Queue.Requeues);
    }

    [TestMethod]
    public async Task OpenCircuitFailsPromptlyWithoutSchedulingDecisionButOrdinaryWorkRecovers()
    {
        foreach (bool decision in new[] { true, false })
        {
            await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(
                backend, true, circuit: () => new NoReplayWorkerTests.OpenCircuit(1000, 1));
            using var response = await gateway.Post(decision ? Path : NoReplayWorkerTests.Path, Body, new())
                .WaitAsync(TimeSpan.FromMilliseconds(decision ? 700 : 4000));
            Assert.AreEqual(decision ? HttpStatusCode.ServiceUnavailable : HttpStatusCode.OK, response.StatusCode);
            await Task.Delay(1200); // Past the actual circuit retry delay: no deferred first send.
            Assert.AreEqual(decision ? 0 : 1, backend.Requests.Count);
            Assert.AreEqual(decision ? 0 : 1, gateway.Queue.Requeues);
        }
    }

    private sealed class ObservedCircuit : NoReplayWorkerTests.HealthyCircuit
    {
        internal readonly TaskCompletionSource Checked = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _checks;
        public override int GetMsToNextRetry()
        {
            Checked.TrySetResult();
            return Interlocked.Increment(ref _checks) == 1 ? 300 : 0;
        }
    }

    [TestMethod]
    public async Task DisconnectAtOpenCircuitNeverDefersADecisionFirstSend()
    {
        await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var circuit = new ObservedCircuit();
        await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(backend, true, circuit: () => circuit);
        using var caller = await gateway.ConnectPost(Path, Body);
        await circuit.Checked.Task.WaitAsync(TimeSpan.FromSeconds(3));
        caller.Dispose();
        await Task.Delay(700); // Circuit recovered; the ordinary control above proves that path sends.
        Assert.AreEqual(0, backend.Requests.Count);
        Assert.AreEqual(0, gateway.Queue.Requeues);
    }

    [TestMethod]
    public async Task DisconnectAfterBackendAcceptanceCannotFailOverOrReplay()
    {
        foreach (bool decision in new[] { true, false })
        {
            var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var first = new WireServer(async _ =>
            {
                entered.TrySetResult();
                await release.Task.WaitAsync(TimeSpan.FromSeconds(5));
                return new WireReply(503, AllowDisconnect: true);
            });
            await using var gateway = await IngressWorkerPolicyTests.Gateway.Start(first, true, additionalBackends: [second]);
            using var caller = await gateway.ConnectPost(decision ? Path : NoReplayWorkerTests.Path, Body);
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(3));
            caller.Dispose();
            release.TrySetResult();
            await Task.Delay(500);
            Assert.AreEqual(1, first.Requests.Count); // Accepted work may still complete/bill.
            Assert.AreEqual(decision ? 0 : 1, second.Requests.Count);
            Assert.AreEqual(0, gateway.Queue.Requeues);
        }
    }

    [TestMethod]
    public async Task DecisionCannotBePersistedRequeuedOrRecovered()
    {
        foreach (bool decision in new[] { true, false })
        {
            string path = decision ? Path : NoReplayWorkerTests.Path;
            await using var backend = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var fixture = await NoReplayWorkerTests.WorkerFixture.Create([backend], false, path: path, body: Body);
            if (decision)
            {
                Assert.ThrowsException<ProxyErrorException>(() => fixture.Requeue.DelayAsync(fixture.Request, 1));
                Assert.ThrowsException<ProxyErrorException>(() => new RequestDataDtoV1(fixture.Request));
                Assert.ThrowsException<InvalidOperationException>(() =>
                    new RequestDataDtoV1 { Method = "POST", Path = path }.PopulateInto(new RequestData()));
            }
            else
            {
                var dto = new RequestDataDtoV1(fixture.Request);
                var recovered = new RequestData();
                dto.PopulateInto(recovered);
                Assert.AreEqual(path, recovered.Path);
                fixture.Requeue.DelayAsync(fixture.Request, 1);
                await fixture.Queue.Enqueued.Task.WaitAsync(TimeSpan.FromSeconds(3));
                fixture.Request.Requeued = false;
            }
            using var result = await fixture.Send();
            Assert.AreEqual(HttpStatusCode.OK, result.StatusCode);
            Assert.AreEqual(1, backend.Requests.Count);
        }
    }
}
