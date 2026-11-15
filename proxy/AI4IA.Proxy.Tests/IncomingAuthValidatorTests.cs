using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class IncomingAuthValidatorTests
{
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
    public void ParseRejectsOauthWithoutTrustedSigningKeys()
    {
        var validator = new IncomingAuthValidator();

        var error = Assert.ThrowsException<InvalidOperationException>(
            () => validator.Parse(
                "enabled=true;mode=oauth2;issuer=https://login.microsoftonline.com/example/v2.0;audience=api://ai4ia"));

        StringAssert.Contains(error.Message, "Failed to parse authentication configuration");
        Assert.IsNotNull(error.InnerException);
        StringAssert.Contains(error.InnerException.Message, "OIDC/JWKS");
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
