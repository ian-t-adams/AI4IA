using System.Security.Cryptography;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using SimpleL7Proxy;
using SimpleL7Proxy.Config;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class IncomingAuthValidatorTests
{
    private const string Issuer = "https://login.microsoftonline.com/11111111-2222-4333-8444-555555555555/v2.0";
    private const string Audience = "api://unit-test-audience";
    private const string AllowedClientId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

    [TestMethod]
    public void ParseUsesConfiguredKeyHeader()
    {
        var validator = new IncomingAuthValidator();

        validator.Parse(
            "enabled=true;mode=key;header=Ocp-Apim-Subscription-Key");

        Assert.IsTrue(validator.ValidateAuthViaKey);
        Assert.AreEqual(
            "Ocp-Apim-Subscription-Key",
            validator.ValidateAuthViaKeyHeader);
    }

    [TestMethod]
    public void ParseRejectsMissingOrPlaceholderOauthInputs()
    {
        foreach (var config in new[]
        {
            "enabled=true;mode=oauth2;issuer=;audience=api://unit-test-audience;allowedclientids=" + AllowedClientId,
            $"enabled=true;mode=oauth2;issuer={Issuer};audience=;allowedclientids={AllowedClientId}",
            $"enabled=true;mode=oauth2;issuer={Issuer};audience=api://example;allowedclientids={AllowedClientId}",
            $"enabled=true;mode=oauth2;issuer={Issuer};audience={Audience},api://other;allowedclientids={AllowedClientId}",
            $"enabled=true;mode=oauth2;issuer={Issuer};audience={Audience};allowedclientids=placeholder",
            $"enabled=true;mode=oauth2;issuer={Issuer};audience={Audience};allowedclientids={AllowedClientId};header=S7P-KEY",
            $"enabled=true;mode=unknown;issuer={Issuer};audience={Audience};allowedclientids={AllowedClientId}"
        })
        {
            var validator = new IncomingAuthValidator();
            var error = Assert.ThrowsException<InvalidOperationException>(() => validator.Parse(config));
            StringAssert.Contains(error.Message, "Failed to parse authentication configuration");
            Assert.IsNotNull(error.InnerException);
        }
    }

    [TestMethod]
    public async Task BearerValidationRejectsInvalidTokenAndClientBindings()
    {
        foreach (var defect in new[] { "issuer", "audience", "signature", "expired", "client", "algorithm" })
        {
            using var trustedRsa = RSA.Create(2048);
            using var untrustedRsa = RSA.Create(2048);
            var trustedKey = new RsaSecurityKey(trustedRsa) { KeyId = "trusted-key" };
            var validator = CreateValidator(Issuer, Audience, AllowedClientId, trustedKey);
            var token = CreateToken(
                trustedKey,
                defect == "signature" ? untrustedRsa : trustedRsa,
                defect == "issuer" ? Issuer.Replace("/v2.0", "/other", StringComparison.Ordinal) : Issuer,
                defect == "audience" ? "api://wrong-audience" : Audience,
                defect == "expired" ? DateTime.UtcNow.AddMinutes(-5) : DateTime.UtcNow.AddMinutes(5),
                defect == "client" ? "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff" : AllowedClientId,
                defect == "algorithm" ? SecurityAlgorithms.RsaSha384 : SecurityAlgorithms.RsaSha256);

            var (isValid, _, _) = await validator.ValidateBearerTokenAsync(token);
            Assert.IsFalse(isValid, $"The {defect} token must be refused.");
        }
    }

    [TestMethod]
    public async Task BearerStartupRequiresMatchingIssuerAndSigningKeys()
    {
        using var rsa = RSA.Create(2048);
        var key = new RsaSecurityKey(rsa) { KeyId = "trusted-key" };
        var validator = CreateValidator(Issuer, Audience, AllowedClientId, key);

        await validator.EnsureTrustedConfigurationAsync(CancellationToken.None);

        var noKeys = new IncomingAuthValidator
        {
            ConfigurationManager = new StaticConfigurationManager(new OpenIdConnectConfiguration
            {
                Issuer = Issuer
            })
        };
        noKeys.Parse(
            $"enabled=true;mode=oauth2;header=Authorization;issuer={Issuer};audience={Audience};allowedclientids={AllowedClientId}");
        await Assert.ThrowsExceptionAsync<InvalidOperationException>(
            () => noKeys.EnsureTrustedConfigurationAsync(CancellationToken.None));

        var wrongIssuer = new IncomingAuthValidator
        {
            ConfigurationManager = new StaticConfigurationManager(new OpenIdConnectConfiguration
            {
                Issuer = Issuer + "/unexpected",
                SigningKeys = { key }
            })
        };
        wrongIssuer.Parse(
            $"enabled=true;mode=oauth2;header=Authorization;issuer={Issuer};audience={Audience};allowedclientids={AllowedClientId}");
        await Assert.ThrowsExceptionAsync<InvalidOperationException>(
            () => wrongIssuer.EnsureTrustedConfigurationAsync(CancellationToken.None));
    }

    [TestMethod]
    public async Task OnlyValidatedAllowedBearerCanBindAnAttempt()
    {
        using var rsa = RSA.Create(2048);
        var key = new RsaSecurityKey(rsa) { KeyId = "trusted-key" };
        var validator = CreateValidator(Issuer, Audience, AllowedClientId, key);
        var token = CreateToken(key, rsa, Issuer, Audience, DateTime.UtcNow.AddMinutes(5), AllowedClientId);
        var (isValid, _, appId) = await validator.ValidateBearerTokenAsync(token);
        Assert.IsTrue(isValid);
        Assert.AreEqual(AllowedClientId, appId);

        var request = new RequestData(
            "request-id",
            Guid.NewGuid(),
            "mid",
            "/ai4ia-attempts-v1/openai/responses",
            "POST",
            DateTime.UtcNow,
            new Dictionary<string, string>());
        request.Headers[NoReplayAttempt.RequestHeader] =
            "ai4ia-one-attempt-v1.0123456789abcdef0123456789abcdef." + new string('a', 64);

        Assert.ThrowsException<ProxyErrorException>(() =>
            NoReplayAttempt.BindAuthenticated(request, false, new ProxyConfig()));
        NoReplayAttempt.BindAuthenticated(request, isValid, new ProxyConfig());
        Assert.IsNotNull(request.NoReplay);
    }

    private static IncomingAuthValidator CreateValidator(
        string issuer,
        string audience,
        string allowedClientId,
        SecurityKey signingKey)
    {
        var validator = new IncomingAuthValidator
        {
            ConfigurationManager = new StaticConfigurationManager(new OpenIdConnectConfiguration
            {
                Issuer = issuer,
                SigningKeys = { signingKey }
            })
        };
        validator.Parse(
            $"enabled=true;mode=oauth2;header=Authorization;issuer={issuer};audience={audience};allowedclientids={allowedClientId}");
        return validator;
    }

    private static string CreateToken(
        SecurityKey trustedKey,
        RSA signingRsa,
        string issuer,
        string audience,
        DateTime expires,
        string clientId,
        string signingAlgorithm = SecurityAlgorithms.RsaSha256)
    {
        var signingKey = ReferenceEquals(signingRsa, ((RsaSecurityKey)trustedKey).Rsa)
            ? trustedKey
            : new RsaSecurityKey(signingRsa) { KeyId = "untrusted-key" };
        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = issuer,
            Audience = audience,
            Expires = expires,
            Claims = new Dictionary<string, object>
            {
                ["ver"] = "2.0",
                ["azp"] = clientId
            },
            SigningCredentials = new SigningCredentials(signingKey, signingAlgorithm)
        });
    }

    private sealed class StaticConfigurationManager(OpenIdConnectConfiguration configuration)
        : IConfigurationManager<OpenIdConnectConfiguration>
    {
        public Task<OpenIdConnectConfiguration> GetConfigurationAsync(CancellationToken cancel) =>
            Task.FromResult(configuration);

        public void RequestRefresh()
        {
        }
    }
}

[TestClass]
public sealed class RequestDataModelDetectionTests
{
    [TestMethod]
    public void ConstructorExtractsAzureNativeDeploymentFromPath()
    {
        using var request = new RequestData(
            "request-id",
            Guid.NewGuid(),
            "mid",
            "/openai/deployments/gpt-5.4-example-eastus2-glbl/chat/completions?api-version=2025-04-01-preview",
            "POST",
            DateTime.UtcNow,
            new Dictionary<string, string>());

        Assert.AreEqual("gpt-5.4-example-eastus2-glbl", request.Model);
    }
}
