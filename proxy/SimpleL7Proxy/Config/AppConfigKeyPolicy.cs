using System.Collections.Frozen;
using System.Globalization;

namespace SimpleL7Proxy.Config;

/// <summary>How <see cref="IAppConfigKeyPolicy"/> treats one downloaded App Configuration setting.</summary>
internal enum AppConfigKeyDecision
{
    Allowed,
    KeyNotReviewed,
    ValueOutOfRange,
}

/// <summary>Decides whether a key and value downloaded from Azure App Configuration may be applied.</summary>
internal interface IAppConfigKeyPolicy
{
    AppConfigKeyDecision Evaluate(string key, string value);
}

/// <summary>
/// AI4IA: default-deny policy for settings downloaded from Azure App Configuration.
/// <para>
/// Upstream applies every <c>Warm:</c> and <c>Cold:</c> key, and an App Configuration value
/// takes precedence over the Container App environment. App Configuration write access would
/// therefore be proxy administration: one write could turn off inbound authentication, add an
/// ingress key, point a backend host elsewhere (it then inherits that host's environment API
/// key), or rewrite the header strip, disallow and logging policy. AI4IA authors all of that
/// in Bicep. Only the refresh sentinel and two reviewed request limits, which Bicep does not
/// set, are applied, and each limit only within a reviewed range: never shorter than the API
/// waits for a call, never longer than the deployed 20-minute timeout. Every other key keeps
/// its current value, including all <c>Cold:</c> keys, backend host and route keys, the
/// circuit-breaker settings, and unknown keys.
/// </para>
/// <para>
/// The circuit-breaker settings are deliberately not reviewed. A breaker reads them only when
/// it is constructed, so a warm write lies dormant until the next restart or scale-out. The
/// parent breaker gates all ingress before authentication and counts probe timeouts, so a
/// lower threshold or a longer timeslice can throttle every request; a safe band would depend
/// on the probe interval and host count. They stay in the environment.
/// </para>
/// </summary>
internal sealed class AppConfigKeyPolicy : IAppConfigKeyPolicy
{
    private const string WarmPrefix = "Warm:";
    private const int MaxLoggedKeys = 20;
    private const int MaxLoggedKeyLength = 128;

    /// <summary>
    /// Floor for <c>Request:DefaultTimeout</c>, in milliseconds. ProxyWorker gives each backend
    /// attempt until the earlier of the TTL deadline and now plus this timeout to return its
    /// response headers. The API gives up on a proxied call after at most 180 s without a
    /// response (<c>gateway_image_timeout_seconds</c>; chat and audio wait 120 s), so a lower
    /// value could abandon a call the API is still waiting for.
    /// </summary>
    internal const int MinTimeoutMs = 180_000;

    /// <summary>
    /// Ceiling for <c>Request:DefaultTimeout</c>: the deployed 20-minute default, which Bicep does
    /// not override. App Configuration can shorten the wait, never lengthen it.
    /// </summary>
    internal const int MaxTimeoutMs = 1_200_000;

    /// <summary>
    /// Floor for <c>Request:DefaultTTLSecs</c>: the deployed default, which Bicep does not
    /// override. The TTL runs from enqueue, so queue wait and requeue delays spend it. App
    /// Configuration can lengthen it, never shorten it.
    /// </summary>
    internal const int MinTtlSecs = 300;

    /// <summary>
    /// Ceiling for <c>Request:DefaultTTLSecs</c>: the deployed 20-minute timeout, so a request
    /// never waits longer than that already allows. This is far below the 2,147,483 s at which
    /// <c>RequestData.CalculateExpiration</c>'s <c>TTL * 1000</c> overflows and every request
    /// expires on arrival.
    /// </summary>
    internal const int MaxTtlSecs = 1_200;

    /// <summary>The warm key paths App Configuration may set, each with its value rule.</summary>
    private static readonly (string KeyPath, Func<string, bool> IsValid)[] Reviewed =
    [
        ("Sentinel", _ => true),
        ("Request:DefaultTimeout", value => IsWholeNumberIn(value, MinTimeoutMs, MaxTimeoutMs)),
        ("Request:DefaultTTLSecs", value => IsWholeNumberIn(value, MinTtlSecs, MaxTtlSecs)),
    ];

    /// <summary>The only policy production composes.</summary>
    internal static AppConfigKeyPolicy Default { get; } = new();

    private readonly FrozenDictionary<string, Func<string, bool>> _rules =
        Reviewed.ToFrozenDictionary(rule => rule.KeyPath, rule => rule.IsValid, StringComparer.OrdinalIgnoreCase);

    private AppConfigKeyPolicy()
    {
    }

    internal IReadOnlyCollection<string> AllowedWarmKeyPaths => _rules.Keys;

    /// <summary>
    /// A setting applies only when its key is exactly <c>Warm:</c> plus a reviewed key path,
    /// compared the way the loader resolves keys (ordinal, case-insensitive; no prefix match
    /// and no trimming), and its value passes that key's rule.
    /// </summary>
    public AppConfigKeyDecision Evaluate(string key, string value)
    {
        if (!key.StartsWith(WarmPrefix, StringComparison.OrdinalIgnoreCase)
            || !_rules.TryGetValue(key[WarmPrefix.Length..], out var isValid))
        {
            return AppConfigKeyDecision.KeyNotReviewed;
        }

        return isValid(value) ? AppConfigKeyDecision.Allowed : AppConfigKeyDecision.ValueOutOfRange;
    }

    /// <summary>
    /// A bounded, printable list of refused key names for the log. Values are never passed
    /// here: a refused value can be a credential.
    /// </summary>
    internal static string DescribeKeys(IEnumerable<string> keys)
    {
        var distinct = keys
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(key => key, StringComparer.OrdinalIgnoreCase)
            .ToList();
        var shown = string.Join(", ", distinct.Take(MaxLoggedKeys).Select(Printable));
        return distinct.Count > MaxLoggedKeys
            ? $"{shown} (+{distinct.Count - MaxLoggedKeys} more)"
            : shown;
    }

    // Plain digits only, which the loader parses identically. Signs, spaces, decimals and the
    // loader's arithmetic expressions are refused rather than interpreted twice.
    private static bool IsWholeNumberIn(string value, int min, int max) =>
        int.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out var number)
        && number >= min
        && number <= max;

    private static string Printable(string key)
    {
        var bounded = key.Length > MaxLoggedKeyLength ? key[..MaxLoggedKeyLength] + "..." : key;
        return string.Concat(bounded.Select(character => char.IsControl(character) ? '?' : character));
    }
}
