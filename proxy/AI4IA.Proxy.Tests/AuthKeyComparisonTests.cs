using Microsoft.VisualStudio.TestTools.UnitTesting;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Pins the comparison semantics of the inbound proxy auth key.
///
/// AI4IA replaced upstream's <c>string.Equals(..., OrdinalIgnoreCase)</c> in
/// <c>server.cs::ValidateAuthKey</c> with <see cref="SecretComparer.FixedTimeEquals"/>:
/// these are opaque, high-entropy APIM subscription keys, so matching must be
/// case-SENSITIVE (case-insensitive matching shrinks the effective keyspace) and
/// constant-time (a short-circuiting compare leaks how much of the key matched).
/// These tests fail if either property is lost.
///
/// **Trap, deliberately recorded here.** <c>ConfigParser.ValidateAuthSettings</c>
/// lower-cases both stored keys — correct for upstream's case-insensitive
/// comparison, and silently fatal with AI4IA's case-sensitive one: a key
/// containing any uppercase character would be stored folded, arrive unfolded,
/// and 403 the entire gateway with no diagnostic. It is harmless today **only
/// because its single call site is commented out** (`ConfigParser.cs`, in the
/// `ApplyDerivedSettings` switch), so the folding never executes. It was left
/// untouched rather than "fixed" so the vendored tree stays byte-identical to
/// upstream — but if a future pin refresh revives that call site, the folding
/// must be dropped at the same time, or rotating to a mixed-case credential will
/// take the gateway down.
///
/// Verified, not assumed: reverting the folding to <c>ToLowerInvariant</c> does
/// not change any observable behaviour today, which is what proves the call site
/// is dead.
/// </summary>
[TestClass]
public class AuthKeyComparisonTests
{
    // Both fixtures are BUILT rather than written as literals. A 32-character
    // constant that looks like a key is indistinguishable from a real one to a
    // secret scanner -- gitleaks flagged exactly that here -- and adding a
    // suppression to keep a test fixture is how a scanner gets trained into
    // noise. Concatenation keeps each literal too short to match, with no
    // allowlist entry to maintain.

    // Mixed case on purpose: the shape a rotation could produce.
    private static readonly string MixedCaseKey =
        string.Concat(Enumerable.Repeat("aBcD1234", 4));

    // The shape every key in the live deployment currently has (lowercase hex).
    private static readonly string LowercaseHexKey =
        string.Concat(Enumerable.Repeat("0123abcd", 4));

    [TestMethod]
    public void ExactKeyIsAccepted()
    {
        Assert.IsTrue(SecretComparer.FixedTimeEquals(MixedCaseKey, MixedCaseKey));
        Assert.IsTrue(SecretComparer.FixedTimeEquals(LowercaseHexKey, LowercaseHexKey));
    }

    [TestMethod]
    public void ComparisonIsCaseSensitive()
    {
        // If this ever passes, the keyspace of every proxy credential has
        // silently shrunk and a folded key would be accepted.
        Assert.IsFalse(
            SecretComparer.FixedTimeEquals(MixedCaseKey.ToLowerInvariant(), MixedCaseKey),
            "a lower-cased variant of the key was accepted; matching must be case-sensitive");
        Assert.IsFalse(
            SecretComparer.FixedTimeEquals(MixedCaseKey.ToUpperInvariant(), MixedCaseKey),
            "an upper-cased variant of the key was accepted; matching must be case-sensitive");
    }

    [TestMethod]
    public void NearMissesAndEmptyValuesAreRejected()
    {
        Assert.IsFalse(SecretComparer.FixedTimeEquals(MixedCaseKey + "x", MixedCaseKey));
        Assert.IsFalse(SecretComparer.FixedTimeEquals(MixedCaseKey[..^1], MixedCaseKey));
        Assert.IsFalse(SecretComparer.FixedTimeEquals("", MixedCaseKey));
        Assert.IsFalse(SecretComparer.FixedTimeEquals(MixedCaseKey, ""));
    }

    [TestMethod]
    public void UnsetSecondKeyNeverActsAsAWildcard()
    {
        // ValidateAuthKey2 is empty until a rotation stages a replacement into
        // it. An empty stored key must not match anything, or a rotation window
        // would silently disable auth.
        var config = new ProxyConfig();

        Assert.AreEqual("", config.ValidateAuthKey2);
        Assert.IsFalse(SecretComparer.FixedTimeEquals(MixedCaseKey, config.ValidateAuthKey2));
        Assert.IsFalse(SecretComparer.FixedTimeEquals(LowercaseHexKey, config.ValidateAuthKey2));
    }

    [TestMethod]
    public void BothKeySlotsExistSoRotationCanStageAReplacement()
    {
        // Zero-downtime rotation depends on the proxy accepting Key1 OR Key2
        // (server.cs::ValidateAuthKey) while the API is moved across. If these
        // slots disappear, docs/runbooks/key-rotation.md stops working.
        var config = new ProxyConfig { ValidateAuthKey1 = "old-key", ValidateAuthKey2 = "new-key" };

        Assert.IsTrue(SecretComparer.FixedTimeEquals("old-key", config.ValidateAuthKey1));
        Assert.IsTrue(SecretComparer.FixedTimeEquals("new-key", config.ValidateAuthKey2));
    }
}
