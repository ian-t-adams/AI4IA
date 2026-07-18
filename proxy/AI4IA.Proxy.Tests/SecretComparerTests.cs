using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class SecretComparerTests
{
    [TestMethod]
    public void FixedTimeEqualsReturnsTrueForIdenticalSecrets()
    {
        Assert.IsTrue(SecretComparer.FixedTimeEquals("S3cr3t-Key-Value", "S3cr3t-Key-Value"));
    }

    [TestMethod]
    public void FixedTimeEqualsIsCaseSensitive()
    {
        // API/subscription keys are opaque high-entropy secrets, not
        // case-insensitive identifiers: a case-folded guess must not match.
        Assert.IsFalse(SecretComparer.FixedTimeEquals("S3cr3t-Key-Value", "s3cr3t-key-value"));
    }

    [TestMethod]
    public void FixedTimeEqualsReturnsFalseForDifferentSecrets()
    {
        Assert.IsFalse(SecretComparer.FixedTimeEquals("S3cr3t-Key-Value", "totally-different-value"));
    }

    [TestMethod]
    public void FixedTimeEqualsReturnsFalseForDifferentLengthSecrets()
    {
        Assert.IsFalse(SecretComparer.FixedTimeEquals("short", "much-longer-value"));
    }

    [TestMethod]
    public void FixedTimeEqualsReturnsFalseWhenEitherValueIsNull()
    {
        Assert.IsFalse(SecretComparer.FixedTimeEquals(null, "S3cr3t-Key-Value"));
        Assert.IsFalse(SecretComparer.FixedTimeEquals("S3cr3t-Key-Value", null));
        Assert.IsFalse(SecretComparer.FixedTimeEquals(null, null));
    }

    [TestMethod]
    public void FixedTimeEqualsReturnsFalseForEmptyVsNonEmpty()
    {
        Assert.IsFalse(SecretComparer.FixedTimeEquals(string.Empty, "S3cr3t-Key-Value"));
    }

    [TestMethod]
    public void FixedTimeEqualsReturnsTrueForBothEmpty()
    {
        Assert.IsTrue(SecretComparer.FixedTimeEquals(string.Empty, string.Empty));
    }
}
