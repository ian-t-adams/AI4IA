using System.Collections.Frozen;

namespace SimpleL7Proxy.Config;

/// <summary>Decides whether a key downloaded from Azure App Configuration may be applied.</summary>
internal interface IAppConfigKeyPolicy
{
    bool IsAllowed(string key);
}

/// <summary>
/// AI4IA: default-deny policy for keys downloaded from Azure App Configuration.
/// <para>
/// Upstream applies every <c>Warm:</c> and <c>Cold:</c> key, and an App Configuration value
/// takes precedence over the Container App environment. App Configuration write access would
/// therefore be proxy administration: one write could turn off inbound authentication, add an
/// ingress key, point a backend host elsewhere (it then inherits that host's environment API
/// key), or rewrite the header strip, disallow and logging policy. AI4IA authors all of that
/// in Bicep. Only the refresh sentinel and a reviewed set of warm operational settings, none of
/// which Bicep sets, are applied. Every other key keeps its environment value, including all
/// <c>Cold:</c> keys, backend host and route keys, and unknown keys.
/// </para>
/// </summary>
internal sealed class AppConfigKeyPolicy : IAppConfigKeyPolicy
{
    private const string WarmPrefix = "Warm:";
    private const int MaxLoggedKeys = 20;
    private const int MaxLoggedKeyLength = 128;

    /// <summary>
    /// The warm key paths App Configuration may set. The sentinel drives refresh; the others
    /// tune circuit-breaker sensitivity and request timeouts. None of them has an
    /// authentication, routing, header, logging, identity or retry-ownership effect.
    /// </summary>
    internal static readonly IReadOnlyList<string> ReviewedWarmKeyPaths =
    [
        "Sentinel",
        "CircuitBreaker:ErrorThreshold",
        "CircuitBreaker:Timeslice",
        "Request:DefaultTimeout",
        "Request:DefaultTTLSecs",
    ];

    /// <summary>The only policy production composes.</summary>
    internal static AppConfigKeyPolicy Default { get; } = new(ReviewedWarmKeyPaths);

    private readonly FrozenSet<string> _allowedWarmKeyPaths;

    private AppConfigKeyPolicy(IEnumerable<string> allowedWarmKeyPaths) =>
        _allowedWarmKeyPaths = allowedWarmKeyPaths.ToFrozenSet(StringComparer.OrdinalIgnoreCase);

    internal IReadOnlySet<string> AllowedWarmKeyPaths => _allowedWarmKeyPaths;

    /// <summary>
    /// True only for <c>Warm:</c> followed by an allowed key path. Both parts compare the way
    /// the loader resolves keys (ordinal, case-insensitive); there is no prefix match or
    /// trimming, and no other mode.
    /// </summary>
    public bool IsAllowed(string key) =>
        key.StartsWith(WarmPrefix, StringComparison.OrdinalIgnoreCase)
        && _allowedWarmKeyPaths.Contains(key[WarmPrefix.Length..]);

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

    private static string Printable(string key)
    {
        var bounded = key.Length > MaxLoggedKeyLength ? key[..MaxLoggedKeyLength] + "..." : key;
        return string.Concat(bounded.Select(character => char.IsControl(character) ? '?' : character));
    }
}
