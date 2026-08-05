using System.Reflection;
using System.Text.Json;
using Microsoft.VisualStudio.TestTools.UnitTesting;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Proves the startup configuration event cannot publish a credential.
///
/// On boot the proxy emits a single "Configuration loaded" custom event
/// containing EVERY resolved config value (ConfigFactory.OutputEnvVars). That is
/// the one payload where all secrets meet all sinks: the default event client
/// writes it to eventslog.json, and Application Insights custom-event properties
/// receive it when telemetry is enabled.
///
/// Redaction there was a substring heuristic over the key path
/// (connectionstring/password/secret/token/apikey/sas). AI4IA binds the deployed
/// proxy-ingress APIM subscription key to <c>ValidateAuthKey1</c> →
/// <c>Profiles:Auth:Key1</c> (infra/modules/gateway.bicep), which matches none of
/// those words, so the live key was written out verbatim.
///
/// These tests are canary-based rather than assertions about one property name:
/// a unique value is planted in every string option that declares itself
/// <c>Secret</c>, and the whole serialized snapshot must not contain it. A new
/// secret added without the flag fails <see cref="EverySecretOptionIsMaskedWholly"/>,
/// and the key-path list below fails if the auth keys stop being marked.
/// </summary>
[TestClass]
public class ConfigRedactionTests
{
    private const string Canary = "CANARY-a1b2c3d4e5f6-SECRET-VALUE";

    /// Key paths that carry credentials and must never appear in the snapshot.
    private static readonly string[] MustBeSecret =
    [
        "Profiles:Auth:Key1",
        "Profiles:Auth:Key2",
    ];

    private static IEnumerable<PropertyInfo> SecretStringOptions() =>
        typeof(ProxyConfig)
            .GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .Where(p => p.PropertyType == typeof(string)
                        && p.CanWrite
                        && p.GetCustomAttribute<ConfigOptionAttribute>()?.Secret == true);

    private static ProxyConfig ConfigWithCanaryInEverySecret()
    {
        var config = new ProxyConfig();
        var planted = 0;
        foreach (var prop in SecretStringOptions())
        {
            prop.SetValue(config, Canary);
            planted++;
        }
        // Without this the masking assertions below would pass vacuously if the
        // Secret flags were ever stripped -- the exact regression they exist for.
        Assert.IsTrue(
            planted >= MustBeSecret.Length,
            $"expected at least {MustBeSecret.Length} secret string options, planted {planted}");
        return config;
    }

    [TestMethod]
    public void DeclaredSecretKeyPathsAreActuallyMarkedSecret()
    {
        var marked = typeof(ProxyConfig)
            .GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .Select(p => p.GetCustomAttribute<ConfigOptionAttribute>())
            .Where(a => a is { Secret: true })
            .Select(a => a!.KeyPath)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

        foreach (var keyPath in MustBeSecret)
        {
            Assert.IsTrue(
                marked.Contains(keyPath),
                $"{keyPath} carries a credential but is no longer marked Secret, so the " +
                "startup event would publish it in the clear.");
        }
    }

    [TestMethod]
    public void EverySecretOptionIsMaskedWholly()
    {
        var snapshot = ConfigFactory.BuildConfigSnapshot(ConfigWithCanaryInEverySecret());

        foreach (var (_, keyPath, display) in snapshot)
        {
            Assert.AreNotEqual(
                Canary, display,
                $"{keyPath} published its raw value into the startup event.");
        }

        foreach (var keyPath in MustBeSecret)
        {
            var entry = snapshot.Single(e =>
                string.Equals(e.KeyPath, keyPath, StringComparison.OrdinalIgnoreCase));
            Assert.AreEqual(
                "****", entry.Display,
                $"{keyPath} must be masked WHOLLY: a partial mask still leaks key material.");
        }
    }

    [TestMethod]
    public void SerializedSnapshotContainsNoSecretMaterial()
    {
        // The strongest form of the check: whatever shape the payload takes, the
        // secret's bytes must not be in it -- not even a prefix/suffix fragment.
        var snapshot = ConfigFactory.BuildConfigSnapshot(ConfigWithCanaryInEverySecret());
        var json = JsonSerializer.Serialize(
            snapshot.ToDictionary(e => $"{e.Mode}:{e.KeyPath}", e => e.Display));

        StringAssert.DoesNotMatch(json, new System.Text.RegularExpressions.Regex(Regex(Canary)));
        Assert.IsFalse(json.Contains(Canary[..8], StringComparison.Ordinal),
            "a leading fragment of the secret reached the serialized event");
        Assert.IsFalse(json.Contains(Canary[^8..], StringComparison.Ordinal),
            "a trailing fragment of the secret reached the serialized event");

        static string Regex(string literal) => System.Text.RegularExpressions.Regex.Escape(literal);
    }

    [TestMethod]
    public void NonSecretOptionsAreStillReadable()
    {
        // Guard against over-masking: the event's purpose is operator diagnosis,
        // and these two legitimately contain "key" without being credentials.
        var config = new ProxyConfig { Port = 8080, PriorityKeyHeader = "S7P-PRIORITY" };
        var snapshot = ConfigFactory.BuildConfigSnapshot(config);

        Assert.AreEqual("8080", Find(snapshot, "Server:Port"));
        Assert.AreEqual("S7P-PRIORITY", Find(snapshot, "Request:Headers:PriorityKeyHeader"));

        static string Find(
            IReadOnlyList<(ConfigMode Mode, string KeyPath, string Display)> snapshot, string keyPath)
            => snapshot.Single(e =>
                string.Equals(e.KeyPath, keyPath, StringComparison.OrdinalIgnoreCase)).Display;
    }
}
