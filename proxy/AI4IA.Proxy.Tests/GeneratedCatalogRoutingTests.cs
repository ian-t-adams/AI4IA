using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Xml;
using System.Xml.Linq;
using Newtonsoft.Json.Linq;
using SimpleL7Proxy.Proxy;

namespace AI4IA.Proxy.Tests;

// Executes generated catalog expressions and HTTP policy against loopback only.
// Realtime checks evaluate generated handshake conditions, not a live WebSocket.
[TestClass]
[DoNotParallelize]
public sealed class GeneratedCatalogRoutingTests
{
    [DataTestMethod]
    [DataRow(true)]
    [DataRow(false)]
    public async Task RuntimeAndGaGatesSurviveGeneratedVersionedAndLegacyChains(bool gaOnly)
    {
        foreach (bool enabled in new[] { false, true })
        {
            await using var provider = new WireServer(_ => Task.FromResult(new WireReply(200)));
            var bundle = await Generate(enabled, gaOnly, provider.Url);
            var evaluate = ApimPolicyHarness.CompilePolicies(
                bundle.Catalog.Values.Concat([bundle.Attempts, bundle.Legacy, bundle.Ga, bundle.Preview]));
            string deployment = bundle.TextDeployment;
            string operation = $"/openai/deployments/{deployment}/chat/completions";
            foreach (bool versioned in new[] { false, true })
            {
                int before = provider.Requests.Count;
                string path = (versioned ? NoReplayAttempt.RoutePrefix : "") + operation;
                var harness = Project(Signed(path, deployment, versioned), bundle, evaluate, versioned);
                await harness.Run();
                Assert.AreEqual(enabled ? 200 : 404, harness.Context.Response.StatusCode);
                Assert.AreEqual(enabled ? 1 : 0, provider.Requests.Count - before);
                Assert.AreEqual(enabled ? 1 : 0, harness.Sends);
                CollectionAssert.AreEqual(bundle.Catalog.Keys.ToArray(),
                    harness.VisitedFragments.Where(bundle.Catalog.ContainsKey).ToArray());
                Assert.AreEqual(enabled ? 1 : 0, ((JObject)harness.Context.Variables["selectedBackends"]).Count);
                if (enabled)
                {
                    var sent = provider.Requests.Last();
                    Assert.AreEqual(operation, sent.Path);
                    Assert.IsFalse(sent.Headers.ContainsKey(NoReplayAttempt.RequestHeader));
                    Assert.IsFalse(sent.Headers.ContainsKey(NoReplayAttempt.ProofHeader));
                    if (versioned)
                        Assert.IsTrue(harness.Context.Response.Headers.ContainsKey(NoReplayAttempt.AckHeader));
                }
            }

            foreach (bool ga in new[] { false, true })
            {
                var policy = ga ? bundle.Ga : bundle.Preview;
                var context = new ApimContext();
                context.Api.Path = ApimPolicyHarness.RuntimeApiPath(ga ? "openai/v1/realtime" : "openai/realtime");
                context.Request.Url = new ApimUrl(
                    $"/openai/{(ga ? "v1/" : "")}realtime?{(ga ? "model" : "deployment")}={bundle.VoiceDeployment}");
                var selected = policy.Element("inbound")!.Element("choose")!.Elements("when")
                    .Where(node => (bool)evaluate(node.Attribute("condition")!.Value, context)).ToArray();
                Assert.AreEqual(enabled && (ga || !gaOnly) ? 1 : 0, selected.Length);
                Assert.AreEqual("404", policy.Descendants("otherwise").Single()
                    .Descendants("set-status").Single().Attribute("code")!.Value);
            }

            int accepted = provider.Requests.Count;
            foreach (string unsupported in new[]
            {
                $"/openai/deployments/{deployment}/audio/speech",
                $"/openai/v1/realtime?model={bundle.VoiceDeployment}",
            })
            {
                var denied = Project(Signed(NoReplayAttempt.RoutePrefix + unsupported, deployment, true),
                    bundle, evaluate, true);
                await denied.Run();
                Assert.AreEqual(400, denied.Context.Response.StatusCode);
                Assert.AreEqual(0, denied.Sends);
                Assert.AreEqual(0, denied.VisitedFragments.Count,
                    "Unsupported attempts operations must fail before catalog or backend work.");
            }
            Assert.AreEqual(accepted, provider.Requests.Count);
        }
    }

    private static ApimPolicyHarness Project(
        WireRequest request, Bundle bundle, Func<string, ApimContext, object> evaluate, bool versioned) =>
        new(request)
        {
            PolicyProjection = versioned ? bundle.Attempts : bundle.Legacy,
            GeneratedCatalog = bundle.Catalog,
            ExpressionEvaluator = evaluate,
        };

    private static WireRequest Signed(string path, string deployment, bool versioned)
    {
        byte[] body = """{"messages":[{"role":"user","content":"synthetic fixture"}]}"""u8.ToArray();
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["x-LLMModel"] = deployment,
            ["Ocp-Apim-Subscription-Key"] = versioned ? NoReplayWorkerTests.Key : NoReplayWorkerTests.LegacyKey,
        };
        if (versioned)
        {
            string value = NoReplayWorkerTests.Header(body);
            headers[NoReplayAttempt.RequestHeader] = value;
            headers[NoReplayAttempt.ProofHeader] = Convert.ToHexStringLower(HMACSHA256.HashData(
                Encoding.UTF8.GetBytes(NoReplayWorkerTests.Key),
                Encoding.UTF8.GetBytes($"{value}\nPOST\n{path}\n{deployment}")));
        }
        return new WireRequest(path, "HTTP/1.1", headers, body);
    }

    private static async Task<Bundle> Generate(bool enabled, bool gaOnly, string providerUrl)
    {
        string? root = null;
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "scripts", "gen-gateway-policy.py")))
            {
                root = directory.FullName;
                break;
            }
        Assert.IsNotNull(root, "Repository generator source must be present.");
        // The real generator reads all committed policy templates. Only its input
        // catalog and output destination are synthetic; no repository file is edited.
        const string script = """
            import importlib.util, json, sys
            from pathlib import Path
            path = Path(sys.argv[1]) / "scripts" / "gen-gateway-policy.py"
            spec = importlib.util.spec_from_file_location("gateway_fixture", path)
            gen = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(gen)
            enabled, ga_only = sys.argv[2] == "true", sys.argv[3] == "true"
            def entry(name, category, active=True):
                return {"name": name, "format": "OpenAI", "category": category, "runtimeEnabled": active,
                    "deployments": [{"region": "eastus2", "sku": "GlobalStandard", "version": "fixture", "capacity": 10}]}
            voice = entry("fixture-voice", "realtime", enabled)
            if ga_only:
                voice["requiredRealtimeProtocol"] = "ga"
            models = {
                "naming": {"subscriptionToken": "fixture", "skuShort": {"GlobalStandard": "glbl"}},
                "regions": {"eastus2": {"dataZone": "US"}},
                "catalog": [entry("control", "chat"), entry("control-voice", "realtime"),
                    entry("fixture-text", "chat", enabled), voice],
            }
            from tempfile import TemporaryDirectory
            with TemporaryDirectory(prefix="ai4ia-generated-routing-") as tmp:
                gen.MODELS_PATH = Path(tmp) / "models.json"
                gen.MODELS_PATH.write_text(json.dumps(models), encoding="utf-8")
                setup, catalog = gen.generate_endpoint_policies()
                legacy, _ = gen.generate_priority_policies()
                fragments = dict(zip(gen.CATALOG_FRAGMENT_IDS, catalog))
                fragments[gen.SETUP_FRAGMENT_ID] = setup
                print(json.dumps({
                    "fragments": fragments, "legacy": legacy, "attempts": gen.generate_attempts_policy(legacy),
                    "preview": gen.generate_realtime_policy(models), "ga": gen.generate_realtime_policy(models, ga=True),
                    "text": gen.deployment_name(model="fixture-text", subscription_token="fixture",
                        region="eastus2", sku="GlobalStandard", sku_short=models["naming"]["skuShort"]),
                    "voice": gen.deployment_name(model="fixture-voice", subscription_token="fixture",
                        region="eastus2", sku="GlobalStandard", sku_short=models["naming"]["skuShort"]),
                }))
            """;
        var start = new ProcessStartInfo("python")
        {
            RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false,
        };
        start.ArgumentList.Add("-c");
        start.ArgumentList.Add(script);
        start.ArgumentList.Add(root);
        start.ArgumentList.Add(enabled ? "true" : "false");
        start.ArgumentList.Add(gaOnly ? "true" : "false");
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        try { await process.WaitForExitAsync(deadline.Token); }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            throw new AssertFailedException("Offline policy generation timed out.");
        }
        string output = await stdout;
        Assert.AreEqual(0, process.ExitCode, await stderr);
        Assert.IsTrue(output.Length < 512 * 1024, "Fixture output exceeds the bound.");
        var raw = JObject.Parse(output);
        var catalog = ((JObject)raw["fragments"]!).Properties().ToDictionary(
            prop => prop.Name, prop => ParseXml(prop.Value.Value<string>()!
                .Replace("{{foundry-eastus2-endpoint}}", providerUrl, StringComparison.Ordinal)));
        return new Bundle(catalog, ParseXml(raw.Value<string>("legacy")!), ParseXml(raw.Value<string>("attempts")!),
            ParseXml(raw.Value<string>("preview")!), ParseXml(raw.Value<string>("ga")!),
            raw.Value<string>("text")!, raw.Value<string>("voice")!);
    }

    private static XElement ParseXml(string text)
    {
        using var reader = new XmlTextReader(new StringReader(text))
        {
            Normalization = false, DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null,
        };
        return XElement.Load(reader);
    }

    private sealed record Bundle(
        IReadOnlyDictionary<string, XElement> Catalog, XElement Legacy, XElement Attempts,
        XElement Preview, XElement Ga, string TextDeployment, string VoiceDeployment);
}
