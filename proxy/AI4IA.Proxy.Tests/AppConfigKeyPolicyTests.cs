using System.Collections;
using System.Globalization;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using SimpleL7Proxy;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// AI4IA's default-deny App Configuration key policy, driven through the proxy's real download,
/// bootstrap merge, backend registration and warm refresh. Each refusal is paired with a control
/// that runs the identical fixture through upstream's download (<see cref="UpstreamKeyPolicy"/>)
/// and shows the write would otherwise apply.
/// </summary>
[TestClass]
[DoNotParallelize] // Tests set process environment variables, as the Container App does.
public sealed class AppConfigKeyPolicyTests
{
    private const string EnvironmentIngressKey = "fixture-environment-ingress-key";
    private const string EnvironmentModelKey = "fixture-environment-model-key";
    private const string EnvironmentAttemptsKey = "fixture-environment-attempts-key";
    private const string EnvironmentAvatarsKey = "fixture-environment-avatars-key";

    // Shaped like the optional named host the photo-avatars branch adds to gateway.bicep.
    private const string PhotoAvatarsHost =
        "host=http://127.0.0.1:9;path=/fixture-photo-avatars;stripprefix=false;mode=apim;probe=/;" +
        "processor=OpenAI;api-key-header=Ocp-Apim-Subscription-Key;retryafter=false";

    private const string ExfiltrationHost =
        "host=https://fixture-exfiltration.invalid;mode=apim;probe=/openai/status;processor=OpenAI;" +
        "api-key-header=Ocp-Apim-Subscription-Key;retryafter=false";

    private const string ExfiltrationAvatarsHost =
        "host=https://fixture-exfiltration-avatars.invalid;path=/fixture-photo-avatars;stripprefix=false;" +
        "mode=apim;probe=/;processor=OpenAI;api-key-header=Ocp-Apim-Subscription-Key;retryafter=false";

    private static readonly string[] ReviewedKeyPaths = ["Sentinel", "Request:DefaultTimeout", "Request:DefaultTTLSecs"];

    [TestMethod]
    public void DefaultPolicyAdmitsOnlyTheSentinelAndTheReviewedRequestLimits()
    {
        CollectionAssert.AreEquivalent(ReviewedKeyPaths, AppConfigKeyPolicy.Default.AllowedWarmKeyPaths.ToArray());
        foreach (string keyPath in AppConfigKeyPolicy.Default.AllowedWarmKeyPaths)
        {
            // Each is a warm, non-secret option the loader resolves, and never a backend host key.
            Assert.IsTrue(ConfigMetadata.WarmDescriptorsByKeyPath.TryGetValue(keyPath, out var descriptor), keyPath);
            Assert.IsFalse(descriptor!.Attribute.Secret, keyPath);
            Assert.IsFalse(ConfigParser.IsBackendHostConfigName(keyPath), keyPath);
        }

        // The constructor Program.cs uses composes this policy and no other.
        var options = new ProxyConfig();
        var service = new AppConfigService(NullLogger<AppConfigService>.Instance, options, new DefaultCredential(options));
        Assert.AreSame(AppConfigKeyPolicy.Default, service.KeyPolicy);
    }

    [DataTestMethod]
    [DataRow("Warm:Sentinel", "1", "Allowed")]
    [DataRow("Warm:Request:DefaultTimeout", "600000", "Allowed")]
    [DataRow("Warm:Request:DefaultTTLSecs", "900", "Allowed")]
    [DataRow("warm:request:defaultttlsecs", "900", "Allowed")] // the loader resolves keys case-insensitively
    [DataRow("Warm:CircuitBreaker:ErrorThreshold", "50", "KeyNotReviewed")] // never applies live
    [DataRow("Warm:CircuitBreaker:Timeslice", "60", "KeyNotReviewed")]
    [DataRow("Cold:Sentinel", "1", "KeyNotReviewed")]
    [DataRow("Cold:Request:DefaultTTLSecs", "900", "KeyNotReviewed")]
    [DataRow("ABCD:Request:DefaultTTLSecs", "900", "KeyNotReviewed")]
    [DataRow("Request:DefaultTTLSecs", "900", "KeyNotReviewed")]
    [DataRow("Warm:Request:DefaultTTLSecsX", "900", "KeyNotReviewed")]
    [DataRow("Warm:Request:DefaultTTL", "900", "KeyNotReviewed")]
    [DataRow("Warm: Request:DefaultTTLSecs", "900", "KeyNotReviewed")]
    [DataRow("Warm:", "1", "KeyNotReviewed")]
    [DataRow("Warm:Host1", "host=https://fixture.invalid", "KeyNotReviewed")]
    [DataRow("Warm:Host1-api-key", "fixture", "KeyNotReviewed")]
    [DataRow("Warm:Host-photoavatars", "host=https://fixture.invalid", "KeyNotReviewed")]
    [DataRow("Warm:Path_fixture", "prefix=/x;hosts=Host1", "KeyNotReviewed")]
    [DataRow("Warm:Profiles:Auth:Config", "enabled=false;mode=none", "KeyNotReviewed")]
    [DataRow("Warm:Profiles:Auth:Key2", "fixture", "KeyNotReviewed")]
    [DataRow("Warm:LoadBalancing:MultiPass:MaxAttempts", "5", "KeyNotReviewed")]
    [DataRow("Warm:Request:DisallowedHeaders", "[]", "KeyNotReviewed")]
    [DataRow("Cold:Server:AuthProviderClass", "Fixture", "KeyNotReviewed")]
    [DataRow("Warm:UseOAuth", "true", "KeyNotReviewed")]
    [DataRow("Cold:OAuthAudience", "fixture", "KeyNotReviewed")]
    public void PolicyAdmitsOnlyExactWarmReviewedKeys(string key, string value, string expected) =>
        Assert.AreEqual(Enum.Parse<AppConfigKeyDecision>(expected), AppConfigKeyPolicy.Default.Evaluate(key, value), key);

    [DataTestMethod]
    [DataRow("Warm:Request:DefaultTimeout", "180000", true)]
    [DataRow("Warm:Request:DefaultTimeout", "600000", true)]
    [DataRow("Warm:Request:DefaultTimeout", "1200000", true)]
    [DataRow("Warm:Request:DefaultTimeout", "0180000", true)] // the loader parses leading zeros identically
    [DataRow("Warm:Request:DefaultTimeout", "179999", false)]
    [DataRow("Warm:Request:DefaultTimeout", "1200001", false)]
    [DataRow("Warm:Request:DefaultTimeout", "3600000", false)]
    [DataRow("Warm:Request:DefaultTimeout", "1", false)]
    [DataRow("Warm:Request:DefaultTimeout", "0", false)]
    [DataRow("Warm:Request:DefaultTimeout", "-600000", false)]
    [DataRow("Warm:Request:DefaultTimeout", "+600000", false)]
    [DataRow("Warm:Request:DefaultTimeout", " 600000", false)]
    [DataRow("Warm:Request:DefaultTimeout", "600000 ", false)]
    [DataRow("Warm:Request:DefaultTimeout", "600000.0", false)]
    [DataRow("Warm:Request:DefaultTimeout", "600,000", false)]
    [DataRow("Warm:Request:DefaultTimeout", "60*20*1000", false)] // the loader would evaluate it
    [DataRow("Warm:Request:DefaultTimeout", "99999999999", false)]
    [DataRow("Warm:Request:DefaultTimeout", "", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "300", true)]
    [DataRow("Warm:Request:DefaultTTLSecs", "900", true)]
    [DataRow("Warm:Request:DefaultTTLSecs", "1200", true)]
    [DataRow("Warm:Request:DefaultTTLSecs", "299", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "1201", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "3600", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "1", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "0", false)]
    [DataRow("Warm:Request:DefaultTTLSecs", "2147484", false)] // TTL * 1000 overflows
    [DataRow("Warm:Sentinel", "any sentinel", true)]
    public void ReviewedLimitsAcceptOnlyWholeNumbersInTheirRange(string key, string value, bool allowed) =>
        Assert.AreEqual(
            allowed ? AppConfigKeyDecision.Allowed : AppConfigKeyDecision.ValueOutOfRange,
            AppConfigKeyPolicy.Default.Evaluate(key, value),
            $"{key}={value}");

    [TestMethod]
    public void ReviewedRangesAreAnchoredToTheDeployedDefaults()
    {
        // gateway.bicep authors neither limit (test_proxy_delivery_contracts binds that), so these
        // defaults are the deployed values. App Configuration can shorten the timeout and lengthen
        // the TTL, each at most to the deployed 20-minute timeout.
        var deployed = new ProxyConfig();
        Assert.AreEqual(deployed.Timeout, AppConfigKeyPolicy.MaxTimeoutMs);
        Assert.AreEqual(deployed.DefaultTTLSecs, AppConfigKeyPolicy.MinTtlSecs);
        Assert.AreEqual(deployed.Timeout, AppConfigKeyPolicy.MaxTtlSecs * 1000);
        Assert.IsTrue(AppConfigKeyPolicy.MinTimeoutMs < AppConfigKeyPolicy.MaxTimeoutMs);

        // Writing back the deployed values is always accepted.
        Assert.AreEqual(AppConfigKeyDecision.Allowed, AppConfigKeyPolicy.Default.Evaluate(
            "Warm:Request:DefaultTimeout", deployed.Timeout.ToString(CultureInfo.InvariantCulture)));
        Assert.AreEqual(AppConfigKeyDecision.Allowed, AppConfigKeyPolicy.Default.Evaluate(
            "Warm:Request:DefaultTTLSecs", deployed.DefaultTTLSecs.ToString(CultureInfo.InvariantCulture)));
    }

    [DataTestMethod]
    [DataRow("Warm:Profiles:Auth:Config", "enabled=false;mode=none")]
    [DataRow("Warm:Profiles:Auth:Key2", "fixture-injected-ingress-key")]
    [DataRow("Warm:Request:StripRequestHeaders", "[\"X-Fixture\"]")]
    [DataRow("Warm:Request:DisallowedHeaders", "[\"X-Fixture\"]")]
    [DataRow("Warm:Logging:LogAllRequestHeaders", "true")]
    [DataRow("Warm:Request:DetectModel", "false")]
    [DataRow("Warm:LoadBalancing:MultiPass:MaxAttempts", "5")]
    [DataRow("Warm:LoadBalancing:IterationMode", "MultiPass")]
    [DataRow("Warm:Profiles:User:UseProfiles", "true")]
    [DataRow("Warm:Async:Timeout", "1000")]
    [DataRow("Warm:CircuitBreaker:ErrorThreshold", "1")]
    [DataRow("Warm:CircuitBreaker:Timeslice", "3600")]
    public async Task WarmRefreshRefusesUnreviewedKeysThatUpstreamAppliesLive(string key, string value)
    {
        var descriptor = ConfigMetadata.WarmDescriptorsByKeyPath[key["Warm:".Length..]];
        foreach (bool guarded in new[] { true, false })
        {
            var live = AuthoredOptions();
            string before = Format(descriptor.Property.GetValue(live));
            var (notified, log) = await RefreshAsync(live, Policy(guarded), null, (key, value));
            string after = Format(descriptor.Property.GetValue(live));
            if (guarded)
            {
                Assert.AreEqual(before, after, key);
                CollectionAssert.DoesNotContain(notified, descriptor.ConfigName, key);
                AssertRefusedByName(log, key);
            }
            else
            {
                // Control: upstream applies the same write live and notifies its subscribers.
                Assert.AreNotEqual(before, after, key);
                CollectionAssert.Contains(notified, descriptor.ConfigName, key);
            }
        }
    }

    [DataTestMethod]
    [DataRow("Warm:Request:DefaultTimeout", "1")]
    [DataRow("Warm:Request:DefaultTimeout", "1200001")]
    [DataRow("Warm:Request:DefaultTTLSecs", "1")]
    [DataRow("Warm:Request:DefaultTTLSecs", "1201")]
    [DataRow("Warm:Request:DefaultTTLSecs", "2147484")]
    public async Task WarmRefreshRefusesOutOfRangeLimitsThatUpstreamAppliesLive(string key, string value)
    {
        var descriptor = ConfigMetadata.WarmDescriptorsByKeyPath[key["Warm:".Length..]];
        foreach (bool guarded in new[] { true, false })
        {
            var live = AuthoredOptions();
            string before = Format(descriptor.Property.GetValue(live));
            var (notified, log) = await RefreshAsync(live, Policy(guarded), null, (key, value));
            string after = Format(descriptor.Property.GetValue(live));
            if (guarded)
            {
                Assert.AreEqual(before, after, key);
                CollectionAssert.DoesNotContain(notified, descriptor.ConfigName, key);
                Assert.IsTrue(
                    log.Entries.Any(entry => entry.Contains("outside the reviewed range") && entry.Contains(key)),
                    string.Join("\n", log.Entries));
            }
            else
            {
                // Control: upstream applies the out-of-range value live.
                Assert.AreEqual(value, after, key);
                CollectionAssert.Contains(notified, descriptor.ConfigName, key);
            }
        }
    }

    [DataTestMethod]
    [DataRow("Warm:Request:DefaultTimeout", "600000")]
    [DataRow("Warm:Request:DefaultTTLSecs", "900")]
    public async Task ReviewedLimitsStillRefreshLive(string key, string value)
    {
        var descriptor = ConfigMetadata.WarmDescriptorsByKeyPath[key["Warm:".Length..]];
        var live = AuthoredOptions();
        Assert.AreNotEqual(value, Format(descriptor.Property.GetValue(live)), key);

        var (notified, log) = await RefreshAsync(live, AppConfigKeyPolicy.Default, null, (key, value));

        Assert.AreEqual(value, Format(descriptor.Property.GetValue(live)), key);
        CollectionAssert.Contains(notified, descriptor.ConfigName, key);
        Assert.IsFalse(log.Entries.Any(entry => entry.Contains("key policy refused")), string.Join("\n", log.Entries));
    }

    [TestMethod]
    public async Task AnOutOfRangeTimeoutIsRefusedWhileAnInRangeOneApplies()
    {
        var log = new CapturingLogger<AppConfigService>();
        var live = AuthoredOptions();
        int environmentTimeout = live.Timeout;
        using var store = new AppConfigurationStore(("Warm:Sentinel", "1"));
        var service = store.CreateService(AppConfigKeyPolicy.Default, log);
        await AppConfigurationStore.BootstrapAsync(service, live, new ConfigChangeNotifier(NullLogger<ConfigChangeNotifier>.Instance));

        store.Set(("Warm:Sentinel", "2"), ("Warm:Request:DefaultTimeout", "1"));
        await service.RefreshNowAsync(CancellationToken.None);
        Assert.AreEqual("2", live.Sentinel, "the refresh cycle did not run");
        Assert.AreEqual(environmentTimeout, live.Timeout);
        AssertRefusedByName(log, "Warm:Request:DefaultTimeout");

        store.Set(("Warm:Sentinel", "3"), ("Warm:Request:DefaultTimeout", "600000"));
        await service.RefreshNowAsync(CancellationToken.None);
        Assert.AreEqual("3", live.Sentinel, "the refresh cycle did not run");
        Assert.AreEqual(600000, live.Timeout);

        // A later out-of-range value keeps the current setting, the last in-range value.
        store.Set(("Warm:Sentinel", "4"), ("Warm:Request:DefaultTimeout", "1"));
        await service.RefreshNowAsync(CancellationToken.None);
        Assert.AreEqual("4", live.Sentinel, "the refresh cycle did not run");
        Assert.AreEqual(600000, live.Timeout);
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task BootstrapRefusesOutOfRangeLimitsAndKeepsTheEnvironmentValues(bool guarded)
    {
        using var environment = new EnvironmentScope(
            [.. AuthoredEnvironment(), ("Timeout", "900000"), ("DefaultTTLSecs", "600")]);
        var (options, _, log) = await BootstrapAsync(
            Policy(guarded),
            ("Warm:Sentinel", "1"),
            ("Warm:Request:DefaultTimeout", "1"),
            ("Warm:Request:DefaultTTLSecs", "1"));

        if (guarded)
        {
            Assert.AreEqual(900000, options.Timeout);
            Assert.AreEqual(600, options.DefaultTTLSecs);
            AssertRefusedByName(log, "Warm:Request:DefaultTimeout");
            AssertRefusedByName(log, "Warm:Request:DefaultTTLSecs");
        }
        else
        {
            // Control: upstream starts the proxy with a 1 ms request timeout and a 1 s queue TTL.
            Assert.AreEqual(1, options.Timeout);
            Assert.AreEqual(1, options.DefaultTTLSecs);
        }
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task AppConfigurationCannotLowerTheParentBreakerThreshold(bool guarded)
    {
        using var environment = new EnvironmentScope(AuthoredEnvironment());
        var (options, _, log) = await BootstrapAsync(
            Policy(guarded), ("Warm:Sentinel", "1"), ("Warm:CircuitBreaker:ErrorThreshold", "1"));

        // Program.cs builds the parent breaker once from these options, and server.cs answers
        // every request with 429, before authentication, while it reports backpressure.
        var parent = new CircuitBreaker(Options.Create(options), NullLogger<CircuitBreaker>.Instance, isParent: true);
        try
        {
            if (guarded)
            {
                Assert.AreEqual(new ProxyConfig().CircuitBreakerErrorThreshold, options.CircuitBreakerErrorThreshold);
                Assert.AreEqual(0, parent.GetBackpressureDelay());
                AssertRefusedByName(log, "Warm:CircuitBreaker:ErrorThreshold");
            }
            else
            {
                // Control: upstream accepts the write, and the parent breaker throttles every
                // request with no failure recorded at all.
                Assert.AreEqual(1, options.CircuitBreakerErrorThreshold);
                Assert.IsTrue(parent.GetBackpressureDelay() > 0);
            }
        }
        finally
        {
            parent.Deregister();
        }
    }

    [TestMethod]
    public async Task CircuitBreakerSettingsNeverApplyLiveEvenUpstream()
    {
        var live = AuthoredOptions();
        var running = new CircuitBreaker(Options.Create(live), NullLogger<CircuitBreaker>.Instance, isParent: true);
        CircuitBreaker? next = null;
        try
        {
            // Upstream's download writes the option live...
            await RefreshAsync(live, UpstreamKeyPolicy.Instance, null, ("Warm:CircuitBreaker:ErrorThreshold", "1"));
            Assert.AreEqual(1, live.CircuitBreakerErrorThreshold);
            // ...but the running breaker keeps the threshold it was built with,
            Assert.AreEqual(0, running.GetBackpressureDelay());
            // and only a breaker built later, at the next restart or scale-out, uses it.
            next = new CircuitBreaker(Options.Create(live), NullLogger<CircuitBreaker>.Instance, isParent: true);
            Assert.IsTrue(next.GetBackpressureDelay() > 0);
        }
        finally
        {
            running.Deregister();
            next?.Deregister();
        }
    }

    [TestMethod]
    public void TheTtlCeilingStaysBelowTheExpirationOverflow()
    {
        var now = DateTime.UtcNow;
        var atCeiling = new RequestData { EnqueueTime = now };
        atCeiling.CalculateExpiration(AppConfigKeyPolicy.MaxTtlSecs, "S7PTTL");
        Assert.AreEqual(now.AddSeconds(AppConfigKeyPolicy.MaxTtlSecs), atCeiling.ExpiresAt);

        // Control: above int.MaxValue / 1000 seconds, TTL * 1000 overflows and a request
        // expires the moment it is enqueued.
        var overflowing = new RequestData { EnqueueTime = now };
        overflowing.CalculateExpiration(int.MaxValue / 1000 + 1, "S7PTTL");
        Assert.IsTrue(overflowing.ExpiresAt < now);
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task WarmRefreshCannotPointABackendHostElsewhere(bool guarded)
    {
        var authored = GatewayUpstreamPolicyTests.AuthoredHosts();
        using var environment = new EnvironmentScope(
            ("Host1", authored["Host1"]), ("Host1-api-key", EnvironmentModelKey), ("APPENDHOSTSFILE", "false"));
        var live = new ProxyConfig();
        var hosts = new VersionedHostConfigTests.CapturedHosts();
        ConfigFactory.RegisterBackends(live, null, null, hosts);
        var before = DescribeHosts(hosts);
        Assert.AreEqual(1, before.Count);

        var (_, log) = await RefreshAsync(live, Policy(guarded), hosts, ("Warm:Host1", ExfiltrationHost));

        if (guarded)
        {
            CollectionAssert.AreEqual(before, DescribeHosts(hosts));
            AssertRefusedByName(log, "Warm:Host1");
        }
        else
        {
            // Control: upstream re-registers Host1 from App Configuration, and the replacement
            // inherits the environment's APIM key.
            var host = hosts.Current.Hosts.Single();
            StringAssert.Contains(host.Host, "fixture-exfiltration.invalid");
            Assert.AreEqual(EnvironmentModelKey, host.Config.ApiKey);
        }
    }

    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task EnvironmentAppliesAsBeforeAndTheSameKeysFromAppConfigurationAreRefused(bool guarded)
    {
        using var environment = new EnvironmentScope(AuthoredEnvironment());
        var policy = Policy(guarded);
        (string Key, string Value)[] reviewed =
        [
            ("Warm:Sentinel", "1"),
            ("Warm:Request:DefaultTimeout", "600000"),
            ("Warm:Request:DefaultTTLSecs", "900"),
        ];

        // The environment alone, with only the sentinel and the reviewed limits in the store.
        var (envOnly, envOnlyHosts, _) = await BootstrapAsync(policy, reviewed);
        Assert.AreEqual(GatewayUpstreamPolicyTests.AuthoredLiteral("ValidateAuthConfig"), envOnly.ValidateAuthConfig);
        Assert.AreEqual(EnvironmentIngressKey, envOnly.ValidateAuthKey1);
        Assert.AreEqual(string.Empty, envOnly.ValidateAuthKey2);
        Assert.AreEqual(GatewayUpstreamPolicyTests.AuthoredMaxAttempts(), envOnly.MaxAttempts);
        CollectionAssert.AreEqual(GatewayUpstreamPolicyTests.AuthoredDisallowedHeaders(), envOnly.DisallowedHeaders);
        // The reviewed limits apply at bootstrap.
        Assert.AreEqual(600000, envOnly.Timeout);
        Assert.AreEqual(900, envOnly.DefaultTTLSecs);
        CollectionAssert.AreEqual(
            new[]
            {
                $"Host-photoavatars {EnvironmentAvatarsKey}",
                $"Host1 {EnvironmentModelKey}",
                $"Host2 {EnvironmentAttemptsKey}",
            },
            envOnlyHosts.Select(host => $"{host.Split(' ')[0]} {host.Split(' ')[^1]}").ToArray());
        Assert.IsFalse(envOnlyHosts.Any(host => host.Contains("exfiltration")));

        // The same settings, and the same hosts, arriving from App Configuration with other values.
        (string Key, string Value)[] overrides =
        [
            ("Warm:Profiles:Auth:Config", "enabled=false;mode=none"),
            ("Cold:Profiles:Auth:Key1", "fixture-replaced-ingress-key"),
            ("ABCD:Profiles:Auth:Key2", "fixture-injected-ingress-key"),
            ("Warm:LoadBalancing:MultiPass:MaxAttempts", "5"),
            ("Warm:Request:DisallowedHeaders", "[]"),
            ("Warm:Request:StripRequestHeaders", "[]"),
            ("Warm:Logging:LogAllRequestHeaders", "true"),
            ("Warm:CircuitBreaker:ErrorThreshold", "1"),
            ("Cold:Server:Workers", "64"),
            ("Warm:Host1", ExfiltrationHost),
            ("Warm:Host-photoavatars", ExfiltrationAvatarsHost),
        ];
        var (withStore, withStoreHosts, log) = await BootstrapAsync(policy, [.. reviewed, .. overrides]);

        if (guarded)
        {
            var expected = Describe(envOnly);
            var actual = Describe(withStore);
            var differences = expected.Where(option => actual[option.Key] != option.Value).Select(option => option.Key).ToList();
            Assert.AreEqual(0, differences.Count, "App Configuration changed: " + string.Join(", ", differences));
            CollectionAssert.AreEqual(envOnlyHosts, withStoreHosts);
            foreach (var (key, _) in overrides)
                AssertRefusedByName(log, key);
        }
        else
        {
            // Control: upstream lets App Configuration replace every one of them, cold keys and
            // hosts included, and a replaced host inherits the environment's key.
            Assert.AreEqual("enabled=false;mode=none", withStore.ValidateAuthConfig);
            Assert.AreEqual("fixture-replaced-ingress-key", withStore.ValidateAuthKey1);
            Assert.AreEqual("fixture-injected-ingress-key", withStore.ValidateAuthKey2);
            Assert.AreEqual(5, withStore.MaxAttempts);
            Assert.AreEqual(0, withStore.DisallowedHeaders.Count);
            Assert.AreEqual(0, withStore.StripRequestHeaders.Count);
            Assert.IsTrue(withStore.LogAllRequestHeaders);
            Assert.AreEqual(1, withStore.CircuitBreakerErrorThreshold);
            Assert.AreEqual(64, withStore.Workers);
            CollectionAssert.Contains(withStoreHosts, $"Host1 https://fixture-exfiltration.invalid / {EnvironmentModelKey}");
            Assert.IsTrue(withStoreHosts.Any(host =>
                host.StartsWith("Host-photoavatars https://fixture-exfiltration-avatars.invalid", StringComparison.Ordinal) &&
                host.EndsWith(EnvironmentAvatarsKey, StringComparison.Ordinal)), string.Join("\n", withStoreHosts));
        }
    }

    [TestMethod]
    public async Task RefusedKeysAreLoggedByNameAndNeverByValue()
    {
        var log = new CapturingLogger<AppConfigService>();
        using var store = new AppConfigurationStore(
            ("Warm:Sentinel", "1"),
            ("Warm:Profiles:Auth:Key2", "fixture-refused-value-ingress-key"),
            ("Cold:Profiles:Auth:Key1", "fixture-refused-value-cold-key"),
            ("Warm:Host1-api-key", "fixture-refused-value-host-key"),
            ("Warm:Profiles:Auth:Config", "enabled=false;mode=none;header=fixture-refused-value-header"),
            ("Warm:Request:DefaultTimeout", "fixture-refused-value-timeout"));
        var service = store.CreateService(AppConfigKeyPolicy.Default, log);
        var live = new ProxyConfig();
        await AppConfigurationStore.BootstrapAsync(service, live, new ConfigChangeNotifier(NullLogger<ConfigChangeNotifier>.Instance));
        store.Set(
            ("Warm:Sentinel", "2"),
            ("Warm:Request:DefaultTTLSecs", "900"),
            ("Warm:Path_fixture", "prefix=/fixture-refused-value-route;hosts=Host1"));
        await service.RefreshNowAsync(CancellationToken.None);

        // Positive controls: the same logger records every refusal by name, at bootstrap and on
        // refresh, and the reviewed limit that applied.
        foreach (string key in new[]
        {
            "Warm:Profiles:Auth:Key2", "Cold:Profiles:Auth:Key1", "Warm:Host1-api-key",
            "Warm:Profiles:Auth:Config", "Warm:Request:DefaultTimeout", "Warm:Path_fixture",
        })
        {
            AssertRefusedByName(log, key);
        }

        Assert.AreEqual(900, live.DefaultTTLSecs);
        Assert.IsTrue(log.Entries.Any(entry => entry.Contains("DefaultTTLSecs")), string.Join("\n", log.Entries));
        // No refused value reaches a message or a structured property.
        Assert.IsFalse(
            log.Entries.Any(entry => entry.Contains("fixture-refused-value", StringComparison.OrdinalIgnoreCase)),
            string.Join("\n", log.Entries));
    }

    [TestMethod]
    public async Task RefusalLogThroughTheServiceIsBoundedAndPrintable()
    {
        string longKey = "Warm:A" + new string('x', 200);
        var settings = new List<(string Key, string Value)>
        {
            ("Warm:Sentinel", "1"),
            ("Warm:A\u0007Bell", "fixture"),
            (longKey, "fixture"),
        };
        settings.AddRange(Enumerable.Range(0, 23).Select(index => ($"Warm:Fixture:Key{index:D2}", "fixture")));
        var log = new CapturingLogger<AppConfigService>();
        using var store = new AppConfigurationStore([.. settings]);
        var service = store.CreateService(AppConfigKeyPolicy.Default, log);

        service.Start();
        await service.GetSettingsAsync();

        var refusal = log.Records.Single(record => record.Message.Contains("key policy refused"));
        Assert.AreEqual(25, refusal.Properties["Count"]);
        string keys = (string)refusal.Properties["Keys"]!;
        string[] names = keys.Split(", ");
        Assert.AreEqual(20, names.Length, keys);
        StringAssert.EndsWith(keys, " (+5 more)");
        Assert.IsFalse(keys.Any(char.IsControl), keys);
        Assert.IsFalse(refusal.Message.Any(char.IsControl), refusal.Message);
        CollectionAssert.Contains(names, "Warm:A?Bell");
        CollectionAssert.Contains(names, longKey[..128] + "...");
        Assert.IsFalse(keys.Contains(longKey, StringComparison.Ordinal), "the long key must be truncated");
    }

    [TestMethod]
    public void RefusedKeyListIsBoundedAndPrintable()
    {
        var many = Enumerable.Range(0, 25).Select(index => $"Warm:Fixture:Key{index:D2}").Append("warm:fixture:key00");
        string described = AppConfigKeyPolicy.DescribeKeys(many);
        StringAssert.EndsWith(described, "(+5 more)");
        Assert.AreEqual(20, described.Split(", ").Length);

        string awkward = AppConfigKeyPolicy.DescribeKeys(["Warm:Line\nBreak", "Warm:" + new string('x', 300)]);
        Assert.IsFalse(awkward.Any(char.IsControl), awkward);
        StringAssert.Contains(awkward, "Warm:Line?Break");
        Assert.IsTrue(awkward.Length < 160, awkward);
    }

    internal static IAppConfigKeyPolicy Policy(bool guarded) =>
        guarded ? AppConfigKeyPolicy.Default : UpstreamKeyPolicy.Instance;

    internal static void AssertRefusedByName(CapturingLogger<AppConfigService> log, string key) =>
        Assert.IsTrue(
            log.Entries.Any(entry => entry.Contains("key policy refused") && entry.Contains(key)),
            $"{key} was not reported as refused:\n" + string.Join("\n", log.Entries));

    /// <summary>The authored gateway.bicep proxy environment, as the Container App supplies it.</summary>
    private static (string Name, string? Value)[] AuthoredEnvironment()
    {
        var hosts = GatewayUpstreamPolicyTests.AuthoredHosts();
        return
        [
            ("Host1", hosts["Host1"]),
            ("Host1-api-key", EnvironmentModelKey),
            ("Host2", hosts["Host2"]),
            ("Host2-api-key", EnvironmentAttemptsKey),
            ("Host-photoavatars", PhotoAvatarsHost),
            ("Host-photoavatars-api-key", EnvironmentAvatarsKey),
            ("APPENDHOSTSFILE", "false"),
            ("ValidateAuthConfig", GatewayUpstreamPolicyTests.AuthoredLiteral("ValidateAuthConfig")),
            ("ValidateAuthKey1", EnvironmentIngressKey),
            ("MaxAttempts", GatewayUpstreamPolicyTests.AuthoredLiteral("MaxAttempts")),
            ("DisallowedHeaders", GatewayUpstreamPolicyTests.AuthoredList("DisallowedHeaders")),
            ("StripRequestHeaders", GatewayUpstreamPolicyTests.AuthoredList("StripRequestHeaders")),
            ("LogAllRequestHeaders", GatewayUpstreamPolicyTests.AuthoredLiteral("LogAllRequestHeaders")),
        ];
    }

    private static ProxyConfig AuthoredOptions() => ConfigParser.ApplyEnv(
        AuthoredEnvironment().ToDictionary(setting => setting.Name, setting => setting.Value!, StringComparer.OrdinalIgnoreCase),
        new ProxyConfig());

    /// <summary>Bootstraps like Program.cs: options from the environment and the store, then backends.</summary>
    private static async Task<(ProxyConfig Options, List<string> Hosts, CapturingLogger<AppConfigService> Log)> BootstrapAsync(
        IAppConfigKeyPolicy policy, params (string Key, string Value)[] settings)
    {
        var log = new CapturingLogger<AppConfigService>();
        using var store = new AppConfigurationStore(settings);
        var service = store.CreateService(policy, log);
        service.Start();
        var (_, options) = await ConfigFactory.CreateOptions(service);
        options.Client?.Dispose();
        var hosts = new VersionedHostConfigTests.CapturedHosts();
        ConfigFactory.RegisterBackends(options, null, service.WarmSettings, hosts);
        return (options, DescribeHosts(hosts), log);
    }

    /// <summary>Runs one warm refresh, exactly as the refresh loop does, after a sentinel-only bootstrap.</summary>
    private static async Task<(List<string> Notified, CapturingLogger<AppConfigService> Log)> RefreshAsync(
        ProxyConfig live, IAppConfigKeyPolicy policy, IHostHealthCollection? hosts,
        params (string Key, string Value)[] update)
    {
        var log = new CapturingLogger<AppConfigService>();
        using var store = new AppConfigurationStore(("Warm:Sentinel", "1"));
        var service = store.CreateService(policy, log);
        var notified = new List<string>();
        var notifier = new ConfigChangeNotifier(NullLogger<ConfigChangeNotifier>.Instance);
        notifier.Subscribe((changes, _, _) =>
        {
            notified.AddRange(changes.Select(change => change.PropertyName));
            return Task.CompletedTask;
        }, Array.Empty<string>());
        await AppConfigurationStore.BootstrapAsync(service, live, notifier, hosts);

        store.Set([("Warm:Sentinel", "2"), .. update]);
        await service.RefreshNowAsync(CancellationToken.None);
        Assert.AreEqual("2", live.Sentinel, "the refresh cycle did not run");
        return (notified, log);
    }

    private static List<string> DescribeHosts(VersionedHostConfigTests.CapturedHosts hosts) =>
        hosts.Current.Hosts
            .Select(host => $"{host.Config.ConfigKey} {host.Host} {host.Config.PartialPath} {host.Config.ApiKey}")
            .OrderBy(host => host, StringComparer.Ordinal)
            .ToList();

    private static Dictionary<string, string> Describe(ProxyConfig options) =>
        ConfigMetadata.Descriptors.ToDictionary(
            descriptor => descriptor.Property.Name, descriptor => Format(descriptor.Property.GetValue(options)));

    private static string Format(object? value) => value switch
    {
        null => "<null>",
        string text => text,
        IDictionary map => string.Join(";", map.Keys.Cast<object>()
            .Select(key => $"{key}={map[key]}")
            .OrderBy(item => item, StringComparer.Ordinal)),
        IEnumerable items => string.Join(";", items.Cast<object?>().Select(Format)),
        _ => Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty,
    };
}
