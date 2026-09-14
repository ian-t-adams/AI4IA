using System.Diagnostics;
using System.Reflection;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class TestRunnerContractTests
{
    private const string Probe = "AI4IA_MSTEST_FAILURE_PROBE";

    [TestMethod]
    public void IsolatedFailureProbe()
    {
        if (Environment.GetEnvironmentVariable(Probe) == "fail")
            Assert.Fail("Intentional isolated runner failure.");
    }

    [TestMethod]
    public async Task RunnerRejectsAFailingTestAndZeroDiscoveryWithPassingControl()
    {
        foreach (string mode in new[] { "pass", "fail", "empty" })
        {
            var start = new ProcessStartInfo("dotnet")
            {
                UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
            };
            start.ArgumentList.Add(Assembly.GetExecutingAssembly().Location);
            start.ArgumentList.Add("--filter");
            start.ArgumentList.Add(mode == "empty"
                ? "FullyQualifiedName=AI4IA.DoesNotExist"
                : $"FullyQualifiedName={typeof(TestRunnerContractTests).FullName}.{nameof(IsolatedFailureProbe)}");
            start.ArgumentList.Add("--minimum-expected-tests");
            start.ArgumentList.Add("1");
            start.Environment[Probe] = mode;
            using var process = Process.Start(start)!;
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            try { await process.WaitForExitAsync(timeout.Token); }
            catch (OperationCanceledException)
            {
                process.Kill(entireProcessTree: true);
                throw new AssertFailedException("Isolated test runner timed out.");
            }
            string output = await stdout + await stderr;
            if (mode == "pass")
            {
                Assert.AreEqual(0, process.ExitCode, output);
                StringAssert.Contains(output, "succeeded: 1");
            }
            else
            {
                Assert.AreNotEqual(0, process.ExitCode, output);
                if (mode == "fail") StringAssert.Contains(output, "failed: 1");
            }
        }
    }
}
