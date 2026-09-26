using System.Collections;
using System.Collections.Concurrent;
using System.Net;
using System.Text;
using System.Text.Json;
using Azure.Core;
using Azure.Core.Pipeline;
using Azure.Data.AppConfiguration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// An in-memory App Configuration store behind a real <see cref="ConfigurationClient"/>. Only the
/// HTTP transport is fake, so the proxy's own download, key resolution, bootstrap merge and
/// warm refresh run unchanged.
/// </summary>
internal sealed class AppConfigurationStore : IDisposable
{
    internal const string Endpoint = "https://fixture.azconfig.io";

    private readonly object _gate = new();
    private readonly HttpClient _http;
    private (string Key, string Value)[] _settings;

    internal AppConfigurationStore(params (string Key, string Value)[] settings)
    {
        _settings = settings;
        _http = new HttpClient(new Handler(this));
    }

    /// <summary>Replaces the whole store, as one publish would.</summary>
    internal void Set(params (string Key, string Value)[] settings)
    {
        lock (_gate)
        {
            _settings = settings;
        }
    }

    internal AppConfigService CreateService(IAppConfigKeyPolicy policy, ILogger<AppConfigService>? logger = null)
    {
        var options = new ProxyConfig { AppConfigEndpoint = Endpoint };
        var clientOptions = new ConfigurationClientOptions { Transport = new HttpClientTransport(_http) };
        clientOptions.Retry.MaxRetries = 0;
        var client = new ConfigurationClient(new Uri(Endpoint), new FixtureCredential(), clientOptions);
        return new AppConfigService(
            logger ?? NullLogger<AppConfigService>.Instance, options, new DefaultCredential(options), client, policy);
    }

    /// <summary>Runs the startup download and wires the refresh the way Program.cs does.</summary>
    internal static async Task BootstrapAsync(
        AppConfigService service, ProxyConfig live, ConfigChangeNotifier notifier, IHostHealthCollection? hosts = null)
    {
        service.Start();
        await service.GetSettingsAsync();
        service.RegisterServices(new ServiceCollection(), live);
        service.Notifier = notifier;
        service.HostCollection = hosts;
    }

    public void Dispose() => _http.Dispose();

    private HttpResponseMessage Respond(HttpRequestMessage request)
    {
        (string Key, string Value)[] settings;
        lock (_gate)
        {
            settings = _settings;
        }

        string path = request.RequestUri!.AbsolutePath;
        if (request.Method == HttpMethod.Get && path == "/kv")
        {
            return Json(new Dictionary<string, object?> { ["items"] = settings.Select(Item).ToArray() },
                "application/vnd.microsoft.appconfig.kvset+json");
        }

        if (request.Method == HttpMethod.Get && path.StartsWith("/kv/", StringComparison.Ordinal))
        {
            string key = Uri.UnescapeDataString(path["/kv/".Length..]);
            foreach (var setting in settings)
            {
                if (setting.Key == key)
                    return Json(Item(setting), "application/vnd.microsoft.appconfig.kv+json");
            }

            return new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("{}", Encoding.UTF8, "application/problem+json"),
            };
        }

        throw new InvalidOperationException($"Unexpected App Configuration request: {request.Method} {path}");
    }

    private static Dictionary<string, object?> Item((string Key, string Value) setting) => new()
    {
        ["etag"] = "fixture-etag",
        ["key"] = setting.Key,
        ["label"] = null,
        ["content_type"] = null,
        ["value"] = setting.Value,
        ["tags"] = new Dictionary<string, string>(),
        ["locked"] = false,
        ["last_modified"] = "2026-09-26T00:00:00+00:00",
    };

    private static HttpResponseMessage Json(object body, string mediaType) => new(HttpStatusCode.OK)
    {
        Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, mediaType),
    };

    private sealed class Handler(AppConfigurationStore store) : HttpMessageHandler
    {
        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken) =>
            store.Respond(request);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(store.Respond(request));
    }

    private sealed class FixtureCredential : TokenCredential
    {
        private static readonly AccessToken Token = new("fixture-token", DateTimeOffset.UtcNow.AddHours(1));

        public override AccessToken GetToken(TokenRequestContext requestContext, CancellationToken cancellationToken) => Token;

        public override ValueTask<AccessToken> GetTokenAsync(TokenRequestContext requestContext, CancellationToken cancellationToken) =>
            new(Token);
    }
}

/// <summary>The control policy: accepts every key and value, which is upstream's download behavior.</summary>
internal sealed class UpstreamKeyPolicy : IAppConfigKeyPolicy
{
    internal static readonly UpstreamKeyPolicy Instance = new();

    public AppConfigKeyDecision Evaluate(string key, string value) => AppConfigKeyDecision.Allowed;
}

/// <summary>
/// Records each log entry's formatted message and its structured properties, so a value cannot
/// hide in a property the message template omits.
/// </summary>
internal sealed class CapturingLogger<T> : ILogger<T>
{
    private readonly ConcurrentQueue<Record> _records = new();

    internal sealed record Record(LogLevel Level, string Message, IReadOnlyDictionary<string, object?> Properties);

    internal IReadOnlyCollection<Record> Records => _records;

    /// <summary>Each entry as one string: level, message, then every structured property.</summary>
    internal IReadOnlyList<string> Entries => _records
        .Select(record =>
        {
            var entry = new StringBuilder().Append(record.Level).Append(": ").Append(record.Message);
            foreach (var (name, value) in record.Properties)
                entry.Append(" | ").Append(name).Append('=').Append(value);
            return entry.ToString();
        })
        .ToList();

    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception,
        Func<TState, Exception?, string> formatter)
    {
        var properties = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (state is IEnumerable<KeyValuePair<string, object?>> pairs)
        {
            foreach (var (name, value) in pairs)
                properties[name] = value;
        }

        if (exception != null)
            properties["Exception"] = exception.ToString();
        _records.Enqueue(new Record(logLevel, formatter(state, exception), properties));
    }
}

/// <summary>
/// Sets process environment variables for one test, the way the Container App environment reaches
/// the proxy, and restores them afterwards. Existing backend host and route settings are cleared
/// for the scope, so only the scope's own hosts exist.
/// </summary>
internal sealed class EnvironmentScope : IDisposable
{
    private readonly Dictionary<string, string?> _saved = new(StringComparer.OrdinalIgnoreCase);

    internal EnvironmentScope(params (string Name, string? Value)[] variables)
    {
        var existing = Environment.GetEnvironmentVariables().Cast<DictionaryEntry>()
            .Select(entry => (string)entry.Key)
            .Where(IsHostSetting)
            .ToList();
        foreach (var name in existing)
            Set(name, null);
        foreach (var (name, value) in variables)
            Set(name, value);
    }

    public void Dispose()
    {
        foreach (var (name, value) in _saved)
            Environment.SetEnvironmentVariable(name, value);
    }

    private void Set(string name, string? value)
    {
        _saved.TryAdd(name, Environment.GetEnvironmentVariable(name));
        Environment.SetEnvironmentVariable(name, value);
    }

    private static bool IsHostSetting(string key) =>
        key.StartsWith("Host", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Probe", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("IP", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Api_Key", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Path_", StringComparison.OrdinalIgnoreCase) ||
        key.StartsWith("Path-", StringComparison.OrdinalIgnoreCase) ||
        key.Equals("APPENDHOSTSFILE", StringComparison.OrdinalIgnoreCase);
}
