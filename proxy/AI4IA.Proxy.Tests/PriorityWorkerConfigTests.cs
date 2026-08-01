using Microsoft.VisualStudio.TestTools.UnitTesting;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Pins which environment variable name actually reserves proxy workers.
///
/// AI4IA routes admins into priority band 1 and everyone else into band 2, and
/// relies on <c>PriorityWorkerDict</c> reserving workers for band 1 so operators
/// keep capacity when the app is saturated. The vendored proxy exposes two
/// confusingly similar names: a SINGULAR <c>PriorityWorker</c> string property on
/// <see cref="ProxyConfig"/>, and a PLURAL <c>PriorityWorkers</c> environment key
/// that <c>ConfigParser.ApplyEnv</c> parses into the dictionary. Only the plural
/// one reaches the dictionary; the singular parses, validates, and is discarded.
///
/// infra/modules/gateway.bicep used the singular name, so the reservation was
/// inert and band 1 silently got nothing. These tests fail if the vendored
/// parser's contract changes under a proxy pin refresh, which is the event that
/// would make the bicep name wrong again.
/// </summary>
[TestClass]
public class PriorityWorkerConfigTests
{
    private const string Reservation = "1:2,3:1";

    private static ProxyConfig Apply(Dictionary<string, string> incoming)
        => ConfigParser.ApplyEnv(incoming, new ProxyConfig());

    private static string Render(Dictionary<int, int> dict)
        => string.Join(",", dict.OrderBy(kvp => kvp.Key).Select(kvp => $"{kvp.Key}:{kvp.Value}"));

    [TestMethod]
    public void PluralNameReservesWorkersForTheRequestedBands()
    {
        var config = Apply(new() { ["PriorityWorkers"] = Reservation });

        Assert.AreEqual(Reservation, Render(config.PriorityWorkerDict));
        Assert.AreEqual(2, config.PriorityWorkerDict[1], "band 1 must get its reserved workers");
    }

    [TestMethod]
    public void SingularNameIsSilentlyIgnored()
    {
        // Documents the trap rather than endorsing it: if a future proxy bump
        // starts honouring the singular name this fails, prompting a re-read of
        // the bicep comment that explains why the plural is used.
        var config = Apply(new() { ["PriorityWorker"] = Reservation });

        Assert.AreNotEqual(
            Reservation,
            Render(config.PriorityWorkerDict),
            "the singular name now reaches PriorityWorkerDict -- re-check gateway.bicep");
        Assert.IsFalse(
            config.PriorityWorkerDict.ContainsKey(1),
            "band 1 got a reservation from the singular name, which it never used to");
    }

    [TestMethod]
    public void EmptyValueFallsBackToTheDefaultSoTheDisabledPathIsUnchanged()
    {
        // gateway.bicep always emits the variable and passes "" when priorities
        // are off. That must not zero out the allocation.
        var unset = Render(Apply(new()).PriorityWorkerDict);

        Assert.AreEqual(unset, Render(Apply(new() { ["PriorityWorkers"] = "" }).PriorityWorkerDict));
    }

    [TestMethod]
    public void ReservationsFitWithinTheConfiguredWorkerCount()
    {
        // gateway.bicep defaults Workers to 10 and would reserve 3 of them.
        // Reserving more workers than exist would starve the unreserved bands.
        var config = Apply(new() { ["Workers"] = "10", ["PriorityWorkers"] = Reservation });

        Assert.IsTrue(
            config.PriorityWorkerDict.Values.Sum() < config.Workers,
            $"reserved {config.PriorityWorkerDict.Values.Sum()} of {config.Workers} workers");
    }
}
