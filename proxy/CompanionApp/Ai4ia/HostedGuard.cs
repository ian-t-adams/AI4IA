using CompanionApp.Components.Shared;

namespace CompanionApp.Ai4ia;

/// <summary>
/// AI4IA hosted-mode guards for the vendored CompanionApp. The upstream request,
/// chat, stress and configuration tools are not vendored; these guards make any
/// remaining outbound HTTP path fail closed and keep Event Hubs access on the
/// Container App's managed identity. See proxy/README.md.
/// </summary>
public static class HostedGuard
{
    public const string ConnectionStringVariable = "EVENTHUB_CONNECTIONSTRING";

    public static HttpClient CreateRefusingHttpClient() => new(new RefusingHandler());

    public static void RequireManagedIdentityOnly(IConfiguration configuration)
    {
        var section = configuration.GetSection(EventHubMonitorOptions.SectionName);
        if (!string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable(ConnectionStringVariable)) ||
            !string.IsNullOrWhiteSpace(section["ConnectionString"]) ||
            !string.IsNullOrWhiteSpace(section["CheckpointStorage"]))
        {
            throw new InvalidOperationException(
                "The AI4IA CompanionApp reads Event Hubs with its managed identity only; remove the connection strings.");
        }
    }

    private sealed class RefusingHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            throw new InvalidOperationException("The AI4IA CompanionApp does not send outbound HTTP requests.");
    }
}
