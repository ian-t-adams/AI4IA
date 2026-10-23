using System.Text;
using Newtonsoft.Json.Linq;

namespace AI4IA.Proxy.Tests;

[TestClass]
[DoNotParallelize]
public sealed class DecisionRoutingTests
{
    private static JObject Catalog()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            var path = Path.Combine(directory.FullName, "app", "api", "src", "ai4ia_api", "data", "model_catalog.json");
            if (!File.Exists(path)) continue;
            return JObject.Parse(File.ReadAllText(path));
        }
        throw new AssertFailedException("Decision catalog must exist.");
    }

    private static string Deployment() => Catalog()["models"]!.Single(m => m.Value<string>("category") == "decision")
        ["options"]!.Single().Value<string>("deploymentName")!;

    private static ApimPolicyHarness Policy(WireServer provider, string operation = "systemone", string method = "POST")
    {
        string deployment = Deployment();
        var policy = new ApimPolicyHarness(new WireRequest(
            $"/openai/deployments/{deployment}/{operation}?api-version=caller&tracking=caller",
            "HTTP/1.1",
            new(StringComparer.OrdinalIgnoreCase)
            {
                ["x-LLMModel"] = deployment, ["api-key"] = "caller-secret",
                ["Authorization"] = "Bearer caller-token",
            },
            Encoding.UTF8.GetBytes("""{"model":"caller-model","state":"synthetic","questions":{"q":{"type":"noul","instructions":"Is this synthetic?"}}}"""),
            Method: method), provider)
        {
            UseCatalog = true,
        };
        foreach (string region in Catalog()["models"]!.SelectMany(m => m["options"]!).Select(o => o.Value<string>("region")!).Distinct())
        {
            policy.Context.NamedValues[$"foundry-{region}-endpoint"] = "https://unused.invalid";
            policy.Context.NamedValues[$"foundry-{region}-services-endpoint"] = provider.Url;
        }
        policy.Context.NamedValues["claude-target-endpoint"] = "https://unused-claude.invalid";
        return policy;
    }

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    [DataTestMethod]
    [DataRow(200, false)]
    [DataRow(200, true)]
    [DataRow(429, false)]
    [DataRow(503, false)]
    public async Task NativeDecisionUsesExactRouteAndNeverRequestsReplay(int status, bool eof)
    {
        byte[] native = """{"model":"microsoft-decision-1","answers":{"q":{"type":"noul","noul":0.75}},"usage":{"input_tokens":10,"output_tokens":1}}"""u8.ToArray();
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(
            status, RawBody: native, EofDelimited: eof,
            Headers: new Dictionary<string, string[]>(StringComparer.OrdinalIgnoreCase)
            {
                ["Content-Type"] = ["application/json"],
                ["TOKENPROCESSOR"] = ["untrusted"], ["S7PREQUEUE"] = ["true"],
            })));
        var policy = Policy(provider);
        await policy.Run();
        Assert.AreEqual(status, policy.Context.Response.StatusCode);
        Assert.AreEqual(1, policy.Sends);
        var sent = provider.Requests.Single();
        Assert.AreEqual("/providers/microsoft/v1/systemone", sent.Path);
        Assert.AreEqual(Deployment(), JObject.Parse(Encoding.UTF8.GetString(sent.Body)).Value<string>("model"));
        Assert.IsFalse(sent.Headers.ContainsKey("api-key"));
        Assert.AreNotEqual("Bearer caller-token", sent.Headers["Authorization"]);
        Assert.AreEqual(("https://cognitiveservices.azure.com", (string?)null), policy.Identities.Single());
        Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("TOKENPROCESSOR"));
        Assert.IsFalse(policy.Context.Response.Headers.ContainsKey("S7PREQUEUE"));
        CollectionAssert.AreEqual(native, policy.Context.Response.Body.Bytes);
    }

    [DataTestMethod]
    [DataRow("chat/completions", "POST")]
    [DataRow("systemone/extra", "POST")]
    [DataRow("systemone/", "POST")]
    [DataRow("systemone", "GET")]
    public async Task OtherOperationsAreRefusedBeforeAnyProviderSend(string operation, string method)
    {
        await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
        var invalid = Policy(provider, operation, method);
        await invalid.Run();
        Assert.AreEqual(404, invalid.Context.Response.StatusCode);
        Assert.AreEqual(0, provider.Requests.Count);
        var valid = Policy(provider);
        await valid.Run();
        Assert.AreEqual(1, provider.Requests.Count);
    }
}
