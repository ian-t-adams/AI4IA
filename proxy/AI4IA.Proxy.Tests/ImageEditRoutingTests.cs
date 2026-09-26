using System.Text;
using Newtonsoft.Json.Linq;

namespace AI4IA.Proxy.Tests;

// Executes the committed generated catalog and legacy APIM policy for image
// edits. Loopback servers stand in for the regional Foundry endpoints; this is
// not live routing, quota or Azure policy-compiler evidence.
[TestClass]
[DoNotParallelize]
public sealed class ImageEditRoutingTests
{
    private const string Boundary = "ai4ia-edit-fixture-boundary";
    private const string ApiVersion = "?api-version=2025-04-01-preview";
    private static readonly string MultipartType = $"multipart/form-data; boundary={Boundary}";
    private static readonly byte[] Multipart = BuildMultipart();
    private static readonly byte[] GenerationJson = Encoding.UTF8.GetBytes("""{"prompt":"synthetic fixture","n":1}""");

    [ClassInitialize]
    public static void Initialize(TestContext _) => ApimPolicyHarness.CompileBeforeTimedRequests();

    // JSON-hostile, non-UTF-8 bytes: a PNG signature, NUL/0xFF runs, bare CRLFs and a
    // boundary-looking line inside the file part. Any parse, re-encoding or rebuild
    // of the multipart body changes them.
    private static byte[] BuildMultipart()
    {
        using var stream = new MemoryStream();
        void Text(string value) => stream.Write(Encoding.ASCII.GetBytes(value));
        Text($"--{Boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nmake the sky purple\r\n");
        Text($"--{Boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"image.png\"\r\n");
        Text("Content-Type: image/png\r\n\r\n");
        stream.Write([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0xFF, 0xFE, 0x7B, 0x22]);
        Text("\r\n--not-the-boundary\r\n");
        stream.Write([0xC3, 0x28, 0x00, 0xFF]);
        Text($"\r\n--{Boundary}\r\nContent-Disposition: form-data; name=\"mask\"; filename=\"mask.png\"\r\n");
        Text("Content-Type: image/png\r\n\r\n");
        stream.Write([0x89, 0x50, 0x4E, 0x47, 0x00, 0x00, 0x00, 0xFF]);
        Text($"\r\n--{Boundary}--\r\n");
        return stream.ToArray();
    }

    private static string Root()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "infra", "models.json"))) return directory.FullName;
        throw new AssertFailedException("Catalog unavailable.");
    }

    private static (JObject Model, JObject Option)[] ImageOptions(bool editing)
    {
        var catalog = JObject.Parse(File.ReadAllText(
            Path.Combine(Root(), "app", "api", "src", "ai4ia_api", "data", "model_catalog.json")));
        return catalog["models"]!.OfType<JObject>()
            .Where(model => model.Value<string>("category") == "image" &&
                model["runtimeEnabled"]?.Value<bool>() != false &&
                (model["imageEditing"]?.Value<bool>() == true) == editing)
            .SelectMany(model => model["options"]!.OfType<JObject>().Select(option => (model, option)))
            .ToArray();
    }

    private static ApimPolicyHarness Policy(
        string deployment, string operation, IReadOnlyDictionary<string, WireServer> providers,
        byte[] body, string contentType)
    {
        var request = new WireRequest($"/openai/deployments/{deployment}/images/{operation}{ApiVersion}", "HTTP/1.1",
            new(StringComparer.OrdinalIgnoreCase) { ["x-LLMModel"] = deployment, ["Content-Type"] = contentType },
            body);
        return Harness(request, providers);
    }

    private static ApimPolicyHarness Harness(WireRequest request, IReadOnlyDictionary<string, WireServer> providers)
    {
        var policy = new ApimPolicyHarness(request) { UseCatalog = true };
        var values = policy.Context.NamedValues;
        var source = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "infra", "models.json")));
        foreach (var region in ((JObject)source["regions"]!).Properties())
        {
            string url = providers.TryGetValue(region.Name, out var provider) ? provider.Url : "https://unused.invalid";
            values[$"foundry-{region.Name}-endpoint"] = url;
            values[$"foundry-{region.Name}-services-endpoint"] = url;
        }
        values["claude-target-endpoint"] = "https://unused-claude.invalid";
        return policy;
    }

    private static async Task<Dictionary<string, WireServer>> Providers(Func<string, int>? status = null)
    {
        var source = JObject.Parse(File.ReadAllText(Path.Combine(Root(), "infra", "models.json")));
        var servers = new Dictionary<string, WireServer>();
        foreach (var region in ((JObject)source["regions"]!).Properties())
        {
            string name = region.Name;
            servers[name] = new WireServer(_ => Task.FromResult(new WireReply(status?.Invoke(name) ?? 200)));
        }
        await Task.CompletedTask;
        return servers;
    }

    private static async Task Dispose(Dictionary<string, WireServer> servers)
    {
        foreach (var server in servers.Values) await server.DisposeAsync();
    }

    [TestMethod]
    public async Task EditingRowsForwardTheMultipartBodyByteForByte()
    {
        var options = ImageOptions(editing: true);
        // One or more deployments for each of the five documented editing models.
        Assert.AreEqual(5, options.Select(pair => pair.Model.Value<string>("id")).Distinct().Count());
        foreach (var (model, option) in options)
        {
            string deployment = option.Value<string>("deploymentName")!;
            string region = option.Value<string>("region")!;
            var providers = await Providers();
            try
            {
                var policy = Policy(deployment, "edits", providers, Multipart, MultipartType);
                await policy.Run();
                Assert.AreEqual(200, policy.Context.Response.StatusCode, deployment);
                Assert.AreEqual(1, policy.Sends, deployment);
                var sent = providers[region].Requests.Single();
                Assert.AreEqual($"/openai/deployments/{deployment}/images/edits", sent.Path, deployment);
                CollectionAssert.AreEqual(Multipart, sent.Body, deployment);
                Assert.AreEqual(MultipartType, sent.Headers["Content-Type"], deployment);
                Assert.AreEqual(0, providers.Where(pair => pair.Key != region).Sum(pair => pair.Value.Requests.Count), deployment);
            }
            finally
            {
                await Dispose(providers);
            }
        }
    }

    [TestMethod]
    public async Task NonEditingImageRowsRefuseEditsBeforeAnyRewriteOrSend()
    {
        var options = ImageOptions(editing: false);
        Assert.IsTrue(options.Any(pair => pair.Model.Value<string>("api") == "mai"), "fixture needs a MAI row");
        Assert.IsTrue(options.Any(pair => pair.Model.Value<string>("api") == "bfl"), "fixture needs a BFL row");
        foreach (var (model, option) in options)
        {
            string deployment = option.Value<string>("deploymentName")!;
            // Paired control on the same row: its governed generation still routes.
            foreach (var (operation, expected) in new[] { ("edits", 404), ("generations", 200) })
            {
                var providers = await Providers();
                try
                {
                    var policy = operation == "edits"
                        ? Policy(deployment, operation, providers, Multipart, MultipartType)
                        : Policy(deployment, operation, providers, GenerationJson, "application/json");
                    await policy.Run();
                    Assert.AreEqual(expected, policy.Context.Response.StatusCode, $"{deployment} {operation}");
                    Assert.AreEqual(expected == 200 ? 1 : 0, policy.Sends, $"{deployment} {operation}");
                    Assert.AreEqual(expected == 200 ? 1 : 0, providers.Values.Sum(server => server.Requests.Count));
                    if (expected == 404)
                        StringAssert.Contains(Encoding.UTF8.GetString(policy.Context.Response.Body.Bytes), "operation_not_allowed");
                }
                finally
                {
                    await Dispose(providers);
                }
            }
        }
    }

    [DataTestMethod]
    [DataRow("images/variations")]
    [DataRow("chat/completions")]
    [DataRow("images/edits/extra")]
    public async Task ImageRowsReachOnlyTheirGovernedOperations(string operation)
    {
        var (_, option) = ImageOptions(editing: true).First(pair => pair.Model.Value<string>("id") == "gpt-image-2");
        string deployment = option.Value<string>("deploymentName")!;
        var providers = await Providers();
        try
        {
            var request = new WireRequest($"/openai/deployments/{deployment}/{operation}{ApiVersion}", "HTTP/1.1",
                new(StringComparer.OrdinalIgnoreCase) { ["x-LLMModel"] = deployment }, GenerationJson);
            var policy = Harness(request, providers);
            await policy.Run();
            Assert.AreEqual(404, policy.Context.Response.StatusCode, operation);
            Assert.AreEqual(0, policy.Sends, operation);
            Assert.AreEqual(0, providers.Values.Sum(server => server.Requests.Count), operation);
        }
        finally
        {
            await Dispose(providers);
        }
    }

    [TestMethod]
    public async Task GlobalStandardFailoverReplaysTheIdenticalMultipartBody()
    {
        var global = ImageOptions(editing: true)
            .Where(pair => pair.Model.Value<string>("id") == "gpt-image-2" && pair.Option.Value<string>("sku") == "GlobalStandard")
            .Select(pair => pair.Option).ToArray();
        Assert.AreEqual(2, global.Length);
        var requested = global[0];
        var alternate = global[1];
        string requestedRegion = requested.Value<string>("region")!;
        var providers = await Providers(region => region == requestedRegion ? 429 : 200);
        try
        {
            var policy = Policy(requested.Value<string>("deploymentName")!, "edits", providers, Multipart, MultipartType);
            await policy.Run();
            Assert.AreEqual(200, policy.Context.Response.StatusCode);
            Assert.AreEqual(2, policy.Sends);
            var first = providers[requestedRegion].Requests.Single();
            var second = providers[alternate.Value<string>("region")!].Requests.Single();
            CollectionAssert.AreEqual(Multipart, first.Body);
            CollectionAssert.AreEqual(Multipart, second.Body);
            Assert.AreEqual($"/openai/deployments/{alternate.Value<string>("deploymentName")}/images/edits", second.Path);
            Assert.AreEqual(MultipartType, second.Headers["Content-Type"]);
        }
        finally
        {
            await Dispose(providers);
        }
    }
}
