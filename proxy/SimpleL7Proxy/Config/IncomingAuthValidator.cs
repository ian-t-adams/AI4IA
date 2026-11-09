using Microsoft.IdentityModel.Tokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.JsonWebTokens;
using System.Text.RegularExpressions;

namespace SimpleL7Proxy.Config;

public sealed class IncomingAuthValidator
{
    private static readonly Regex s_tenantIssuer = new(
        @"\Ahttps://login\.microsoftonline\.com/(?<tenant>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/v2\.0\z",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly Regex s_audience = new(
        @"\A[^;,\s<>=]{1,256}\z",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly Regex s_placeholder = new(
        @"(example|placeholder|changeme|change-me|replace|todo|your[-_]|<|>)",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Compiled);

    public string[] Audiences { get; set; } = [];
    public string[] AllowedClientIds { get; set; } = [];
    public int ClockSkewMinutes { get; set; } = 1;
    public bool Enabled { get; set; }
    public string Header { get; set; } = "S7P-KEY";
    public string Issuer { get; set; } = string.Empty;
    public string Mode { get; set; } = "key";
    public bool RequireSignedTokens { get; set; } = true;
    public Dictionary<string, string> RequiredClaims { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public bool ValidateAudience { get; set; } = true;
    public bool ValidateIssuer { get; set; } = true;
    public bool ValidateIssuerSigningKey { get; set; } = true;
    public bool ValidateLifetime { get; set; } = true;
    public IncomingAuthModeEnum ValidateAuthMode { get; set; } = IncomingAuthModeEnum.Key;
    public bool ValidateAuthViaKey { get; set; } = false;
    public bool ValidateAuthViaOauthHeader { get; set; } = false;
    public string ValidateAuthViaKeyHeader { get; set; } = "S7P-KEY";
    public IConfigurationManager<OpenIdConnectConfiguration>? ConfigurationManager { get; set; }

    public TokenValidationParameters validationParameters { get; set; } = null!;

    public void Parse(string ValidateAuthConfig)
    {

        if (string.IsNullOrWhiteSpace(ValidateAuthConfig))
        {
            ValidateAuthViaKey = false;
            ValidateAuthViaOauthHeader = false;
            return;
        }

        try
        {

            FromConfigString(ValidateAuthConfig);
            ValidateAuthViaKeyHeader = Header;

            var normalizedMode = Mode.Trim().ToLowerInvariant();

            ValidateAuthMode = normalizedMode switch
            {
                "key" => IncomingAuthModeEnum.Key,
                "mixed" => IncomingAuthModeEnum.Mixed,
                "none" => IncomingAuthModeEnum.None,
                "oauth2" or "oauth" => IncomingAuthModeEnum.OAuth2,
                _ => throw new FormatException("Unsupported inbound authentication mode.")
            };

            ValidateAuthViaKey = normalizedMode is "key" or "mixed";
            ValidateAuthViaOauthHeader = normalizedMode is "oauth2" or "oauth" or "mixed";
            ValidateAuthViaKeyHeader = string.IsNullOrWhiteSpace(Header)
                ? "S7P-KEY"
                : Header.Trim();

            if (ValidateAuthMode == IncomingAuthModeEnum.Mixed)
            {
                throw new InvalidOperationException(
                    "Mixed inbound authentication is not supported.");
            }

            if (ValidateAuthMode == IncomingAuthModeEnum.OAuth2)
                ConfigureTrustedBearerValidation();

            validationParameters = ToTokenValidationParameters();
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException("Failed to parse authentication configuration", ex);
        }
    }

    private void FromConfigString(string? rawConfig)
    {
        if (string.IsNullOrWhiteSpace(rawConfig))
            return;

        var separator = rawConfig.Contains(';') ? ';' : ',';
        var parts = rawConfig.Split(separator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

        foreach (var part in parts)
        {
            if (string.IsNullOrWhiteSpace(part) || !part.Contains('='))
                continue;

            var idx = part.IndexOf('=');
            var key = part[..idx].Trim();
            var value = part[(idx + 1)..].Trim();

            switch (key.ToLowerInvariant())
            {
                case "enabled":
                    Enabled = bool.TryParse(value, out var enabled) && enabled;
                    break;
                case "mode":
                    Mode = value;
                    break;
                case "header":
                    Header = value;
                    break;
                case "issuer":
                    Issuer = value;
                    break;
                case "audience":
                case "audiences":
                    Audiences = ParseList(value);
                    break;
                case "allowedclientids":
                case "allowedclientid":
                    var configuredIds = ParseList(value);
                    if (configuredIds.Length == 0 ||
                        configuredIds.Any(id => !Guid.TryParse(id, out _)))
                        throw new FormatException("Allowed client IDs must be GUIDs.");
                    AllowedClientIds = configuredIds
                        .Select(id => Guid.Parse(id).ToString("D"))
                        .Distinct(StringComparer.OrdinalIgnoreCase)
                        .ToArray();
                    break;
                case "claim":
                case "requiredclaim":
                    var claimKey = value;
                    var claimValue = string.Empty;
                    var claimIdx = value.IndexOf(':');
                    if (claimIdx >= 0)
                    {
                        claimKey = value[..claimIdx].Trim();
                        claimValue = value[(claimIdx + 1)..].Trim();
                    }

                    if (!string.IsNullOrWhiteSpace(claimKey))
                        RequiredClaims[claimKey] = claimValue;
                    break;
                case "validatelifetime":
                    ValidateLifetime = bool.TryParse(value, out var validateLifetime) && validateLifetime;
                    break;
                case "validateissuer":
                    ValidateIssuer = bool.TryParse(value, out var validateIssuer) && validateIssuer;
                    break;
                case "validateaudience":
                    ValidateAudience = bool.TryParse(value, out var validateAudience) && validateAudience;
                    break;
                case "validatesignature":
                case "requiresignedtokens":
                    RequireSignedTokens = bool.TryParse(value, out var requireSignedTokens) && requireSignedTokens;
                    break;
                case "clockskewminutes":
                    ClockSkewMinutes = int.TryParse(value, out var skew) ? skew : ClockSkewMinutes;
                    break;
            }
        }

        return;
    }

    public IncomingAuthValidator DeepClone()
    {
        return new IncomingAuthValidator
        {
            Enabled = Enabled,
            Mode = Mode,
            Header = Header,
            Issuer = Issuer,
            Audiences = (string[])Audiences.Clone(),
            AllowedClientIds = (string[])AllowedClientIds.Clone(),
            RequiredClaims = new Dictionary<string, string>(RequiredClaims, StringComparer.OrdinalIgnoreCase),
            ValidateIssuer = ValidateIssuer,
            ValidateAudience = ValidateAudience,
            ValidateLifetime = ValidateLifetime,
            ValidateIssuerSigningKey = ValidateIssuerSigningKey,
            RequireSignedTokens = RequireSignedTokens,
            ClockSkewMinutes = ClockSkewMinutes,
            ValidateAuthMode = ValidateAuthMode,
            ValidateAuthViaKey = ValidateAuthViaKey,
            ValidateAuthViaOauthHeader = ValidateAuthViaOauthHeader,
            ValidateAuthViaKeyHeader = ValidateAuthViaKeyHeader,
            ConfigurationManager = ConfigurationManager
        };
    }

    public async Task<(bool isValid, string message, string appid)> ValidateBearerTokenAsync(string token)
    {
        if (string.IsNullOrWhiteSpace(token) || ConfigurationManager is null)
            return (false, "Token validation is unavailable", string.Empty);

        try
        {
            var configuration = await ConfigurationManager
                .GetConfigurationAsync(CancellationToken.None)
                .ConfigureAwait(false);
            if (!string.Equals(configuration.Issuer, Issuer, StringComparison.Ordinal) ||
                configuration.SigningKeys.Count == 0)
                return (false, "Token validation metadata is invalid", string.Empty);

            var parameters = validationParameters.Clone();
            parameters.IssuerSigningKeys = configuration.SigningKeys;
            var result = await new JsonWebTokenHandler()
                .ValidateTokenAsync(token, parameters)
                .ConfigureAwait(false);
            if (!result.IsValid)
            {
                var detail = result.Exception switch
                {
                    SecurityTokenExpiredException => "expired token",
                    SecurityTokenInvalidAudienceException => "invalid audience",
                    SecurityTokenInvalidIssuerException => "invalid issuer",
                    SecurityTokenInvalidSignatureException => "invalid signature",
                    _ => "token validation failed"
                };
                return (false, $"Invalid Auth: {detail}", string.Empty);
            }

            var claims = result.Claims;
            if (!claims.TryGetValue("ver", out var version) ||
                !string.Equals(version?.ToString(), "2.0", StringComparison.Ordinal))
                return (false, "Invalid Auth: unsupported token version", string.Empty);

            var appid = GetClientId(claims);
            if (appid.Length == 0 ||
                !AllowedClientIds.Contains(appid, StringComparer.OrdinalIgnoreCase))
                return (false, "Invalid Auth: unlisted client", appid);

            foreach (var expectedClaim in RequiredClaims)
            {
                if (!claims.TryGetValue(expectedClaim.Key, out var actualValue) ||
                    !string.Equals(actualValue?.ToString(), expectedClaim.Value, StringComparison.OrdinalIgnoreCase))
                    return (false, $"Claim {expectedClaim.Key} is not valid", appid);
            }

            return (true, string.Empty, appid);
        }
        catch (Exception)
        {
            return (false, "Token validation is unavailable", string.Empty);
        }
    }

    public async Task EnsureTrustedConfigurationAsync(CancellationToken cancellationToken)
    {
        if (ValidateAuthMode != IncomingAuthModeEnum.OAuth2)
            return;

        if (ConfigurationManager is null)
            throw new InvalidOperationException("OAuth2 signing-key configuration is unavailable.");

        var configuration = await ConfigurationManager
            .GetConfigurationAsync(cancellationToken)
            .ConfigureAwait(false);
        if (!string.Equals(configuration.Issuer, Issuer, StringComparison.Ordinal) ||
            configuration.SigningKeys.Count == 0)
            throw new InvalidOperationException("OAuth2 issuer or signing-key metadata is invalid.");
    }

    public TokenValidationParameters ToTokenValidationParameters()
    {
        return new TokenValidationParameters
        {
            ValidateIssuer = ValidateIssuer,
            ValidIssuer = Issuer,
            ValidateAudience = ValidateAudience,
            ValidAudiences = Audiences,
            ValidateLifetime = ValidateLifetime,
            ClockSkew = TimeSpan.FromMinutes(ClockSkewMinutes),
            ValidateIssuerSigningKey = ValidateIssuerSigningKey,
            ValidAlgorithms = [SecurityAlgorithms.RsaSha256],
            RequireSignedTokens = RequireSignedTokens,
            RequireExpirationTime = true
        };
    }

    private void ConfigureTrustedBearerValidation()
    {
        if (!Enabled ||
            !string.Equals(Header, "Authorization", StringComparison.OrdinalIgnoreCase) ||
            s_tenantIssuer.Match(Issuer) is not { Success: true } ||
            Audiences.Length != 1 || !s_audience.IsMatch(Audiences[0]) ||
            s_placeholder.IsMatch(Issuer) || s_placeholder.IsMatch(Audiences[0]) ||
            AllowedClientIds.Length == 0 ||
            !ValidateIssuer || !ValidateAudience || !ValidateLifetime ||
            !ValidateIssuerSigningKey || !RequireSignedTokens ||
            ClockSkewMinutes is < 0 or > 5)
        {
            throw new InvalidOperationException(
                "OAuth2 mode requires a tenant issuer, one API audience, signed-token validation and an allowed client ID.");
        }

        var metadataAddress = $"{Issuer}/.well-known/openid-configuration";
        ConfigurationManager ??= new ConfigurationManager<OpenIdConnectConfiguration>(
            metadataAddress,
            new OpenIdConnectConfigurationRetriever(),
            new BoundedOidcDocumentRetriever());
    }

    private static string GetClientId(IDictionary<string, object> claims)
    {
        var azp = claims.TryGetValue("azp", out var authorizedParty) ? authorizedParty?.ToString() : null;
        var appid = claims.TryGetValue("appid", out var applicationId) ? applicationId?.ToString() : null;
        if (!string.IsNullOrWhiteSpace(azp) && !string.IsNullOrWhiteSpace(appid) &&
            !string.Equals(azp, appid, StringComparison.OrdinalIgnoreCase))
            return string.Empty;
        return !string.IsNullOrWhiteSpace(azp) ? azp : appid ?? string.Empty;
    }

    private static string[] ParseList(string value)
    {
        var trimmed = value.Trim();
        if (trimmed.StartsWith('[') && trimmed.EndsWith(']'))
            trimmed = trimmed[1..^1];

        return trimmed.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(x => x.Trim().Trim('"'))
            .Where(x => x.Length > 0)
            .ToArray();
    }
}