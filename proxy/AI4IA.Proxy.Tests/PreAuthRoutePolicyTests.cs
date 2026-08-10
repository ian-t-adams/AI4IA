using System.Net;
using SimpleL7Proxy;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class PreAuthRoutePolicyTests
{
    [DataTestMethod]
    [DataRow(Constants.Health)]
    [DataRow(Constants.HealthDetail)]
    [DataRow(Constants.ForceGC)]
    public async Task LegacyDiagnosticsReturnNotFoundBeforeWorkerDispatch(string path)
    {
        var probeCalls = 0;
        var result = await Server.DispatchPreAuthRouteAsync(
            path,
            _ =>
            {
                probeCalls++;
                return Task.FromResult(HttpStatusCode.OK);
            });

        Assert.IsFalse(result.ContinueToAuthenticatedWorker);
        Assert.AreEqual(HttpStatusCode.NotFound, result.StatusCode);
        Assert.IsNull(result.ProbeType);
        Assert.AreEqual(0, probeCalls);
    }

    [DataTestMethod]
    [DataRow(Constants.Startup, nameof(PreAuthRouteDisposition.Startup))]
    [DataRow(Constants.Liveness, nameof(PreAuthRouteDisposition.Liveness))]
    [DataRow(Constants.Readiness, nameof(PreAuthRouteDisposition.Readiness))]
    public async Task RequiredContainerAppsProbesRemainPublicAndDirect(
        string path,
        string expectedName)
    {
        var expected = Enum.Parse<PreAuthRouteDisposition>(expectedName);
        var observed = new List<PreAuthRouteDisposition>();
        var result = await Server.DispatchPreAuthRouteAsync(
            path,
            route =>
            {
                observed.Add(route);
                return Task.FromResult(HttpStatusCode.OK);
            });

        Assert.IsFalse(result.ContinueToAuthenticatedWorker);
        Assert.AreEqual(HttpStatusCode.OK, result.StatusCode);
        Assert.AreEqual(expectedName, result.ProbeType);
        CollectionAssert.AreEqual(new[] { expected }, observed);
    }

    [TestMethod]
    public async Task ModelTrafficStillContinuesToAuthenticatedWorkerDispatch()
    {
        var probeCalls = 0;
        var result = await Server.DispatchPreAuthRouteAsync(
            "/openai/deployments/example/chat/completions",
            _ =>
            {
                probeCalls++;
                return Task.FromResult(HttpStatusCode.OK);
            });

        Assert.IsTrue(result.ContinueToAuthenticatedWorker);
        Assert.AreEqual(0, probeCalls);
    }
}
