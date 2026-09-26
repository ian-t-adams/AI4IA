using System.Text.Json;
using CompanionApp.Components.Shared;

namespace CompanionApp.Ai4ia;

/// <summary>
/// AI4IA hosted-mode guards for the vendored CompanionApp. The upstream request,
/// chat, stress and configuration tools are not vendored; these guards make any
/// remaining outbound HTTP path fail closed, keep Event Hubs access on the
/// Container App's managed identity, and admit only allow-listed admins.
/// See proxy/README.md.
/// </summary>
public static class HostedGuard
{
    public const string ConnectionStringVariable = "EVENTHUB_CONNECTIONSTRING";
    public const string AdminSectionName = CompanionAppOptions.SectionName + ":Admin";
    // Injected by Container Apps authentication, which drops client-supplied copies.
    public const string PrincipalIdHeader = "X-MS-CLIENT-PRINCIPAL-ID";
    public const string PrincipalHeader = "X-MS-CLIENT-PRINCIPAL";
    private const int MaxPrincipalHeaderBytes = 64 * 1024;

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

    /// <summary>
    /// Reads the admin allow-list. An empty or malformed list refuses startup, so a
    /// console can never run without an explicit admin set.
    /// </summary>
    public static AdminAccess RequireAdminAllowlist(IConfiguration configuration)
    {
        var section = configuration.GetSection(AdminSectionName);
        var groups = ParseIds(section["GroupIds"], "GroupIds");
        var principals = ParseIds(section["PrincipalIds"], "PrincipalIds");
        if (groups.Count == 0 && principals.Count == 0)
        {
            throw new InvalidOperationException(
                "The AI4IA CompanionApp requires at least one admin group or principal object id.");
        }
        return new AdminAccess(groups, principals);
    }

    /// <summary>Rejects every request whose platform-authenticated principal is not an allow-listed admin.</summary>
    public static Func<HttpContext, RequestDelegate, Task> AdminOnly(AdminAccess access) => async (context, next) =>
    {
        if (!access.Allows(context.Request.Headers))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
        await next(context);
    };

    private static HashSet<Guid> ParseIds(string? value, string name)
    {
        var ids = new HashSet<Guid>();
        foreach (var item in (value ?? string.Empty).Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (!Guid.TryParse(item, out var id))
            {
                throw new InvalidOperationException($"CompanionApp admin {name} must be Entra object id GUIDs.");
            }
            ids.Add(id);
        }
        return ids;
    }

    public sealed class AdminAccess
    {
        private readonly HashSet<Guid> _groups;
        private readonly HashSet<Guid> _principals;

        internal AdminAccess(HashSet<Guid> groups, HashSet<Guid> principals)
        {
            _groups = groups;
            _principals = principals;
        }

        /// <summary>
        /// Admits the Entra object id Container Apps authentication injected, or one of its
        /// group claims. Anything missing, malformed or oversized is a denial.
        /// </summary>
        public bool Allows(IHeaderDictionary headers)
        {
            if (!Guid.TryParse(headers[PrincipalIdHeader].ToString(), out var principal))
            {
                return false;
            }
            if (_principals.Contains(principal))
            {
                return true;
            }
            if (_groups.Count == 0)
            {
                return false;
            }
            var encoded = headers[PrincipalHeader].ToString();
            if (encoded.Length == 0 || encoded.Length > MaxPrincipalHeaderBytes)
            {
                return false;
            }
            try
            {
                using var document = JsonDocument.Parse(Convert.FromBase64String(encoded));
                if (!document.RootElement.TryGetProperty("claims", out var claims) || claims.ValueKind != JsonValueKind.Array)
                {
                    return false;
                }
                foreach (var claim in claims.EnumerateArray())
                {
                    if (claim.ValueKind == JsonValueKind.Object &&
                        claim.TryGetProperty("typ", out var type) && type.ValueKind == JsonValueKind.String &&
                        type.GetString() == "groups" &&
                        claim.TryGetProperty("val", out var value) && value.ValueKind == JsonValueKind.String &&
                        Guid.TryParse(value.GetString(), out var group) && _groups.Contains(group))
                    {
                        return true;
                    }
                }
            }
            catch (Exception error) when (error is FormatException or JsonException)
            {
                return false;
            }
            return false;
        }
    }

    private sealed class RefusingHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            throw new InvalidOperationException("The AI4IA CompanionApp does not send outbound HTTP requests.");
    }
}
