using System.Text;
using Newtonsoft.Json.Linq;

namespace AI4IA.Proxy.Tests;

// Executes the committed generated catalog and legacy APIM policy for the rows
// added from the 2026-09-23 subscription evidence. Loopback servers stand in for
// the regional Foundry endpoints; this is not live routing or quota evidence.
[TestClass]
[DoNotParallelize]
public sealed class CatalogModelRowRoutingTests
{
    private static readonly string[] AddedModels =
        ["gpt-6-sol", "gpt-6-luna", "gpt-5.5", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"];

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    private static string Root()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "infra", "models.json"))) return directory.FullName;
        throw new AssertFailedException("Catalog unavailable.");
    }

    private static (JObject Model, JObject Option)[] AddedOptions()
    {
        var catalog = JObject.Parse(File.ReadAllText(
            Path.Combine(Root(), "app", "api", "src", "ai4ia_api", "data", "model_catalog.json")));
        var rows = catalog["models"]!.OfType<JObject>()
            .Where(model => AddedModels.Contains(model.Value<string>("id"))).ToArray();
        Assert.AreEqual(AddedModels.Length, rows.Length);
        return rows.SelectMany(model => model["options"]!.OfType<JObject>().Select(option => (model, option))).ToArray();
    }

    private static bool IsImage(JObject model) => model.Value<string>("category") == "image";

    private static string OperationPath(JObject model, string deployment) =>
        IsImage(model) ? $"/openai/deployments/{deployment}/images/generations" : "/openai/responses";

    // Text rows use the Responses shape the application sends: the proxy stamps
    // x-LLMModel from the body's model. Image rows keep the deployment path.
    private static ApimPolicyHarness Policy(JObject model, string deployment, IReadOnlyDictionary<string, WireServer> providers)
    {
        var body = IsImage(model)
            ? new JObject { ["prompt"] = "synthetic fixture", ["n"] = 1 }
            : new JObject
            {
                ["model"] = deployment,
                ["input"] = new JArray(new JObject { ["role"] = "user", ["content"] = "synthetic fixture" }),
            };
        var request = new WireRequest(OperationPath(model, deployment), "HTTP/1.1",
            new(StringComparer.OrdinalIgnoreCase) { ["x-LLMModel"] = deployment },
            Encoding.UTF8.GetBytes(body.ToString()));
        var policy = new ApimPolicyHarness(request) { UseCatalog = true };
        var values = policy.Context.NamedValues;
        var source = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "infra", "models.json")));
        foreach (var region in ((JObject)source["regions"]!).Properties())
        {
            values[$"foundry-{region.Name}-endpoint"] =
                providers.TryGetValue(region.Name, out var provider) ? provider.Url : "https://unused.invalid";
            values[$"foundry-{region.Name}-services-endpoint"] = "https://unused-services.invalid";
        }
        values["claude-target-endpoint"] = "https://unused-claude.invalid";
        return policy;
    }

    private static void AssertSentTo(JObject model, WireRequest sent, string deployment)
    {
        Assert.AreEqual(OperationPath(model, deployment), sent.Path);
        if (!IsImage(model))
            Assert.AreEqual(deployment, JObject.Parse(Encoding.UTF8.GetString(sent.Body)).Value<string>("model"));
    }

    [TestMethod]
    public async Task AddedRowsReachOnlyTheirRequestedRegionalDeployment()
    {
        var options = AddedOptions();
        // 3 text models x 2 regions x (GlobalStandard + DataZoneStandard) = 12, plus
        // one eastus2 GlobalStandard row per gpt-image-2.5 model: their shared global
        // quota fits a single replica.
        Assert.AreEqual(14, options.Length);
        foreach (var (model, option) in options)
        {
            string deployment = option.Value<string>("deploymentName")!;
            string region = option.Value<string>("region")!;
            await using var eastus2 = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var swedencentral = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var providers = new Dictionary<string, WireServer> { ["eastus2"] = eastus2, ["swedencentral"] = swedencentral };
            var policy = Policy(model, deployment, providers);
            await policy.Run();
            Assert.AreEqual(200, policy.Context.Response.StatusCode, deployment);
            Assert.AreEqual(1, policy.Sends, deployment);
            AssertSentTo(model, providers[region].Requests.Single(), deployment);
            Assert.AreEqual(0, providers.Where(pair => pair.Key != region).Sum(pair => pair.Value.Requests.Count), deployment);
            Assert.IsTrue(policy.Identities.Count > 0 &&
                policy.Identities.All(identity => identity == ("https://cognitiveservices.azure.com", null)), deployment);
        }
    }

    [TestMethod]
    public async Task UndeclaredRegionForAnAddedModelNeverReachesAProvider()
    {
        var (model, option) = AddedOptions().First(pair =>
            pair.Model.Value<string>("id") == "gpt-6-sol" && pair.Option.Value<string>("region") == "eastus2"
            && pair.Option.Value<string>("sku") == "GlobalStandard");
        string declared = option.Value<string>("deploymentName")!;
        string undeclared = declared.Replace("-eastus2-", "-westus-");
        foreach (var (deployment, expected) in new[] { (undeclared, 404), (declared, 200) })
        {
            await using var eastus2 = new WireServer(_ => Task.FromResult(new WireReply(200)));
            await using var westus = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var providers = new Dictionary<string, WireServer> { ["eastus2"] = eastus2, ["westus"] = westus };
            var policy = Policy(model, deployment, providers);
            await policy.Run();
            Assert.AreEqual(expected, policy.Context.Response.StatusCode, deployment);
            Assert.AreEqual(expected == 200 ? 1 : 0, policy.Sends, deployment);
            Assert.AreEqual(expected == 200 ? 1 : 0, eastus2.Requests.Count + westus.Requests.Count, deployment);
        }
    }
}
