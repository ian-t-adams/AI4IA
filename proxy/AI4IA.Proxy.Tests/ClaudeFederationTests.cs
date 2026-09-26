using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json.Linq;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class ClaudeFederationTests
{
    private const string TargetToken = "offline-target-access-token-not-a-real-credential";
    private static string Root()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "infra", "models.json"))) return directory.FullName;
        throw new AssertFailedException("Catalog unavailable.");
    }

    private static string[] Deployments()
    {
        var catalog = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "app", "api", "src", "ai4ia_api", "data", "model_catalog.json")));
        return catalog["models"]!.OfType<JObject>().Where(m => m.Value<string>("deploymentTarget") == "external-claude")
            .SelectMany(m => m["options"]!.Select(o => o.Value<string>("deploymentName")!)).ToArray();
    }

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    private static ApimResponse Token(string? body = null, int status = 200) => new()
    {
        StatusCode = status,
        Headers = new(StringComparer.OrdinalIgnoreCase) { ["Content-Type"] = ["application/json; charset=utf-8"] },
        Body = new ApimBody(Encoding.UTF8.GetBytes(body ?? new JObject
        {
            ["access_token"] = TargetToken, ["token_type"] = "Bearer", ["expires_in"] = 3600,
        }.ToString())),
    };

    private static ApimPolicyHarness Policy(WireServer provider, string? deployment = null, bool enabled = true)
    {
        deployment ??= Deployments()[0];
        var request = new WireRequest("/openai/deployments/" + deployment + "/chat/completions", "HTTP/1.1",
            new(StringComparer.OrdinalIgnoreCase)
            {
                ["x-LLMModel"] = deployment, ["Authorization"] = "Bearer caller-secret",
                ["api-key"] = "caller-provider-key", ["x-api-key"] = "caller-anthropic-key",
                ["x-ai4ia-claude-tenant"] = "caller-tenant", ["x-backend-url"] = "https://attacker.invalid",
            },
            Encoding.UTF8.GetBytes(new JObject
            {
                ["model"] = deployment,
                ["messages"] = new JArray(new JObject { ["role"] = "user", ["content"] = "synthetic" }),
            }.ToString()));
        var policy = new ApimPolicyHarness(request, provider)
        {
            ClaudeEnabled = enabled, UseCatalog = true, ProviderLoopback = provider.Url, TokenResponse = Token(),
        };
        policy.Context.Subscription!.Id = "ai4ia-proxy-models";
        var values = policy.Context.NamedValues;
        values["claude-target-tenant"] = "11111111-1111-4111-8111-111111111111";
        values["claude-app-client"] = "22222222-2222-4222-8222-222222222222";
        values["claude-uami-client"] = "33333333-3333-4333-8333-333333333333";
        values["claude-target-endpoint"] = "https://mf-claude-fixture.services.ai.azure.com";
        values["claude-proxy-subscription"] = "ai4ia-proxy-models";
        var catalog = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "infra", "models.json")));
        foreach (var region in ((JObject)catalog["regions"]!).Properties())
        {
            values[$"foundry-{region.Name}-endpoint"] = "https://ordinary.invalid";
            values[$"foundry-{region.Name}-services-endpoint"] = "https://ordinary-services.invalid";
        }
        return policy;
    }

    [TestMethod]
    public async Task ActualGeneratedCatalogUsesOnlyTheBoundTargetAndTargetToken()
    {
        Assert.AreEqual(5, Deployments().Length);
        foreach (string deployment in Deployments())
        {
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var policy = Policy(provider, deployment);
            await policy.Run();
            Assert.AreEqual(200, policy.Context.Response.StatusCode);
            var sent = provider.Requests.Single();
            Assert.AreEqual("/anthropic/v1/messages", sent.Path);
            Assert.AreEqual("Bearer " + TargetToken, sent.Headers["Authorization"]);
            Assert.AreEqual("2023-06-01", sent.Headers["anthropic-version"]);
            Assert.IsFalse(sent.Headers.ContainsKey("api-key"));
            Assert.IsFalse(sent.Headers.ContainsKey("x-api-key"));
            Assert.AreEqual(deployment, JObject.Parse(Encoding.UTF8.GetString(sent.Body)).Value<string>("model"));
            Assert.AreEqual(1, policy.Identities.Count);
            Assert.AreEqual(("api://AzureADTokenExchange", policy.Context.NamedValues["claude-uami-client"]), policy.Identities.Single());
            var exchange = policy.Exchanges.Single();
            Assert.AreEqual("https://login.microsoftonline.com/" + policy.Context.NamedValues["claude-target-tenant"] + "/oauth2/v2.0/token", exchange.Url);
            StringAssert.Contains(exchange.Body, "scope=https%3A%2F%2Fai.azure.com%2F.default");
            StringAssert.Contains(exchange.Body, "client_assertion=offline-source-assertion-never-forward");
            Assert.IsFalse(exchange.Body.Contains("caller-secret"));
            Assert.IsFalse(exchange.Body.Contains("caller-tenant"));
            Assert.IsFalse(policy.Context.Response.Body.As<string>().Contains(TargetToken));
        }
    }

    [DataTestMethod]
    [DataRow("disabled")]
    [DataRow("subscription")]
    [DataRow("tenant")]
    [DataRow("endpoint")]
    [DataRow("path")]
    [DataRow("model")]
    [DataRow("identity")]
    [DataRow("bounded-generic-chat")]
    public async Task InvalidBindingNeverCallsProviderOrFallsBack(string defect)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var policy = Policy(provider);
        switch (defect)
        {
            case "disabled": policy.ClaudeEnabled = false; break;
            case "subscription": policy.Context.Subscription!.Id = "all-apis"; break;
            case "tenant": policy.Context.NamedValues["claude-target-tenant"] = "common"; break;
            case "endpoint": policy.Context.NamedValues["claude-target-endpoint"] = "https://attacker.invalid"; break;
            case "path": policy.Context.Request.MatchedParameters["path"] = "responses"; break;
            case "model": policy.Context.Request.Headers["x-LLMModel"] = ["not-in-catalog"]; break;
            case "identity": policy.FailIdentity = true; break;
            case "bounded-generic-chat":
                var digest = Convert.ToHexString(SHA256.HashData(policy.Context.Request.Body.Bytes)).ToLowerInvariant();
                string value = "ai4ia-one-attempt-v1." + new string('a', 32) + "." + digest;
                string signed = value + "\nPOST\n" + policy.Context.Request.OriginalUrl.Path + "\n" + policy.Context.Request.Headers["x-LLMModel"][0];
                policy.Context.Request.Headers["x-ai4ia-attempt"] = [value];
                policy.Context.Request.Headers["x-ai4ia-proxy-attempt"] = [
                    Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(NoReplayWorkerTests.LegacyKey), Encoding.UTF8.GetBytes(signed))).ToLowerInvariant(),
                ];
                break;
        }
        await policy.Run();
        Assert.AreNotEqual(200, policy.Context.Response.StatusCode);
        Assert.AreEqual(0, provider.Requests.Count);
        Assert.IsFalse(policy.Identities.Any(i => i.Client is null), "An external failure selected system identity.");
        var valid = Policy(provider);
        await valid.Run();
        Assert.AreEqual(200, valid.Context.Response.StatusCode);
        Assert.AreEqual(1, provider.Requests.Count);
    }

    [DataTestMethod]
    [DataRow("missing")]
    [DataRow("error")]
    [DataRow("expired")]
    [DataRow("too-long")]
    [DataRow("string-expiry")]
    [DataRow("bool-expiry")]
    [DataRow("token-type")]
    [DataRow("scope")]
    [DataRow("malformed")]
    [DataRow("overflow")]
    [DataRow("integer-overflow")]
    [DataRow("token-newline")]
    [DataRow("redirect")]
    [DataRow("timeout")]
    public async Task ExchangeFailuresAreBoundedSanitizedAndHaveNoModelFallback(string defect)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var policy = Policy(provider);
        var body = JObject.Parse(Token().Body.As<string>());
        switch (defect)
        {
            case "missing": body.Remove("access_token"); break;
            case "error": body["error"] = "private-provider-diagnostic"; break;
            case "expired": body["expires_in"] = 0; break;
            case "too-long": body["expires_in"] = 7201; break;
            case "string-expiry": body["expires_in"] = "3600"; break;
            case "bool-expiry": body["expires_in"] = true; break;
            case "token-type": body["token_type"] = "Basic"; break;
            case "scope": body["scope"] = "https://graph.microsoft.com/.default"; break;
            case "token-newline": body["access_token"] = TargetToken + "\r\nInjected: header"; break;
        }
        policy.TokenResponse = defect switch
        {
            "malformed" => Token("not json private-provider-diagnostic"),
            "overflow" => Token(new string('x', 20001)),
            "integer-overflow" => Token("{\"access_token\":\"" + TargetToken + "\",\"token_type\":\"Bearer\",\"expires_in\":" + new string('9', 100) + "}"),
            "redirect" => Token(body.ToString(), 307),
            "timeout" => null,
            _ => Token(body.ToString()),
        };
        await policy.Run();
        Assert.AreEqual(502, policy.Context.Response.StatusCode);
        Assert.AreEqual(0, provider.Requests.Count);
        Assert.AreEqual(1, policy.Exchanges.Count);
        Assert.AreEqual(0, policy.Cache.Count);
        string response = policy.Context.Response.Body.As<string>() + string.Join(",", policy.Context.Response.Headers.Values.SelectMany(v => v));
        Assert.IsFalse(response.Contains("private-provider-diagnostic"));
        Assert.IsFalse(response.Contains("offline-source-assertion"));
        Assert.IsFalse(response.Contains(TargetToken));
        var control = Policy(provider);
        await control.Run();
        Assert.AreEqual(1, provider.Requests.Count);
    }

    [TestMethod]
    public async Task EveryRequestExchangesForExactConfigurationAndIgnoresCachedTokenHints()
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var first = Policy(provider);
        await first.Run();
        var same = Policy(provider);
        same.Cache = first.Cache;
        await same.Run();
        Assert.AreEqual(1, same.Exchanges.Count);
        Assert.AreEqual(1, same.Identities.Count);
        Assert.AreEqual(0, first.Cache.Count);
        foreach (string field in new[] { "claude-target-tenant", "claude-app-client", "claude-uami-client", "claude-target-endpoint" })
        {
            var changed = Policy(provider);
            changed.Cache = new(first.Cache);
            changed.Context.NamedValues[field] = field == "claude-target-endpoint"
                ? "https://mf-claude-alternative.services.ai.azure.com"
                : "44444444-4444-4444-8444-444444444444";
            await changed.Run();
            Assert.AreEqual(1, changed.Exchanges.Count, field);
            Assert.AreEqual(200, changed.Context.Response.StatusCode, field);
        }
        var hinted = Policy(provider);
        hinted.Cache["claude-token"] = "stale-token-from-other-tenant";
        hinted.Context.Variables["managed-id-access-token"] = "caller-token";
        await hinted.Run();
        Assert.AreEqual(1, hinted.Exchanges.Count);
        Assert.AreEqual(200, hinted.Context.Response.StatusCode);
        Assert.AreEqual(TargetToken, hinted.Context.Variables["managed-id-access-token"]);
        Assert.IsFalse(ApimPolicyHarness.ClaudeAuth.Descendants("cache-lookup-value").Any());
        Assert.IsFalse(ApimPolicyHarness.ClaudeAuth.Descendants("cache-store-value").Any());
    }

    [TestMethod]
    public async Task DispatchRechecksExactBackendAfterAuthentication()
    {
        foreach (bool change in new[] { false, true })
        {
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var policy = Policy(provider);
            await policy.Run(context =>
            {
                if (change) ((JObject)((JArray)context.Variables["listBackends"])[0])["url"] = "https://ordinary.invalid/openai";
            });
            Assert.AreEqual(change ? 0 : 1, provider.Requests.Count);
            Assert.AreEqual(change ? 502 : 200, policy.Context.Response.StatusCode);
        }
    }
}
