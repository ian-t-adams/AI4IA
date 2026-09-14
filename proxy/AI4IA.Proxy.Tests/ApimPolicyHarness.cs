using System.Diagnostics;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
using Newtonsoft.Json.Linq;

namespace AI4IA.Proxy.Tests;

// Offline projection, not the Azure policy engine or deployment evidence.
// Expressions are compiled from the actual generated fragments with the SDK's
// existing compiler; unknown instructions/expressions fail rather than skip.
internal sealed class ApimPolicyHarness
{
    private static readonly string PolicyDirectory = FindPolicies();
    private static readonly string[] Sections = ["inbound_pre", "inbound_post", "backend", "outbound", "on_error"];
    internal static readonly Dictionary<string, XElement> Policies = Sections.ToDictionary(
        s => s, s => LoadPolicy(System.IO.Path.Combine(PolicyDirectory, $"simplel7proxy_{s}_32.xml")));
    private static readonly Lazy<Func<string, ApimContext, object>> Evaluator = new(Compile);
    internal static void CompileBeforeTimedRequests() => _ = Evaluator.Value;
    internal readonly ApimContext Context = new();
    internal int Sends;
    internal int Limit = 0;
    private string _phase = "";
    private string _backend = "";
    private string _path = "";

    internal ApimPolicyHarness(WireRequest request, params WireServer[] servers)
    {
        Context.Request.Headers = new(request.Headers.ToDictionary(p => p.Key, p => new[] { p.Value }), StringComparer.OrdinalIgnoreCase);
        Context.Request.Body = new ApimBody(request.Body);
        Context.Request.OriginalUrl = new ApimUrl(request.Path);
        Context.Request.Url = new ApimUrl(request.Path);
        Context.Request.MatchedParameters["path"] = request.Path.Split('?')[0].Replace("/openai/", "");
        _path = request.Path;
        var variables = Context.Variables;
        variables["model"] = request.Headers.GetValueOrDefault("x-LLMModel", "fixture-text").ToLowerInvariant();
        variables["priorityHeaderName"] = "S7PPriorityKey";
        variables["AffinityHeaderName"] = "affinity";
        variables["PolicyCycleCounterHeaderName"] = "x-PolicyCycleCounter";
        variables["authResource"] = "https://cognitiveservices.azure.com/";
        variables["selectedBackends"] = new JObject { ["fixture"] = "fixture" };
        variables["priorityCfg"] = JObject.Parse("""{"3":{"retryCount":2,"requeue":true}}""");
        var backends = new JArray();
        for (int i = 0; i < servers.Length; i++)
            backends.Add(new JObject
            {
                ["label"] = $"fixture-{i}", ["affinity"] = $"fixture-{i}",
                ["url"] = servers[i].Url + "/openai", ["path"] = "openai", ["deployment"] = "fixture-text",
                ["priorityGroup"] = i, ["acceptablePriorities"] = new JArray(1, 2, 3),
                ["timeout"] = 1, ["bufferResponse"] = false, ["auth"] = "MI", ["limitConcurrency"] = "off",
                ["defaultRetryAfter"] = 10,
            });
        variables["listBackends"] = backends;
    }

    internal async Task Run(Action<ApimContext>? beforeBackend = null)
    {
        try
        {
            foreach (string phase in new[] { "inbound_pre", "inbound_post", "backend", "outbound" })
            {
                _phase = phase;
                if (phase == "backend") beforeBackend?.Invoke(Context);
                await Execute(Policies[phase]);
            }
        }
        catch (PolicyReturn) { }
        catch (Exception error) when (error is HttpRequestException or OperationCanceledException or PolicyFault)
        {
            Context.LastError.Reason = "FixtureTransportFailure";
            _phase = "on_error";
            try { await Execute(Policies["on_error"]); }
            catch (PolicyReturn) { }
        }
    }

    internal object Eval(string expression) => Evaluator.Value(expression, Context);
    private string Text(string expression) => expression.StartsWith('@') ? Eval(expression).ToString()! : expression;
    private bool Condition(XElement node) => (bool)Eval(node.Attribute("condition")!.Value);

    private async Task Children(XElement node)
    {
        foreach (var child in node.Elements())
            await Execute(child);
    }

    private async Task Execute(XElement node)
    {
        switch (node.Name.LocalName)
        {
            case "fragment":
            case "when":
            case "otherwise":
                await Children(node);
                break;
            case "choose":
                var branch = node.Elements("when").FirstOrDefault(Condition) ?? node.Element("otherwise");
                if (branch is not null) await Children(branch);
                break;
            case "set-variable":
                string value = node.Attribute("value")!.Value;
                Context.Variables[node.Attribute("name")!.Value] = value.StartsWith('@') ? Eval(value) : value;
                break;
            case "retry":
                int count = int.Parse(node.Attribute("count")!.Value);
                for (int attempt = 0; ; attempt++)
                {
                    try { await Children(node); }
                    catch (Exception error) when (error is HttpRequestException or OperationCanceledException or PolicyFault)
                    {
                        if (attempt >= count || !Condition(node)) throw;
                    }
                    if (attempt >= count || !Condition(node)) break;
                }
                break;
            case "limit-concurrency":
                if (Limit > 0) throw new PolicyFault();
                await Children(node);
                break;
            case "forward-request":
                Sends++;
                using (var handler = new SocketsHttpHandler
                {
                    UseProxy = false, AllowAutoRedirect = bool.Parse(Text(node.Attribute("follow-redirects")!.Value)),
                })
                using (var client = new HttpClient(handler))
                {
                    Assert.AreEqual("1", node.Attribute("http-version")!.Value);
                    var url = _backend + "/" + _path.TrimStart('/');
                    using var request = new HttpRequestMessage(HttpMethod.Post, url);
                    request.Content = new ByteArrayContent(Context.Request.Body.Bytes);
                    foreach (var pair in Context.Request.Headers)
                        if (pair.Key is not ("Host" or "Content-Length" or "Content-Type" or "Connection"))
                            request.Headers.TryAddWithoutValidation(pair.Key, pair.Value);
                    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(double.Parse(Text(node.Attribute("timeout")!.Value))));
                    using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
                    Context.Response.StatusCode = (int)response.StatusCode;
                    Context.Response.Headers = response.Headers.Concat(response.Content.Headers)
                        .ToDictionary(p => p.Key, p => p.Value.ToArray(), StringComparer.OrdinalIgnoreCase);
                    Context.Response.Body = new ApimBody(await response.Content.ReadAsByteArrayAsync(timeout.Token));
                }
                break;
            case "set-backend-service":
                _backend = Text(node.Attribute("base-url")!.Value);
                break;
            case "rewrite-uri":
                _path = Text(node.Attribute("template")!.Value);
                break;
            case "set-header":
                var headers = _phase.StartsWith("inbound") || _phase == "backend"
                    ? Context.Request.Headers : Context.Response.Headers;
                string name = Text(node.Attribute("name")!.Value);
                if (node.Attribute("exists-action")!.Value == "delete") headers.Remove(name);
                else headers[name] = node.Elements("value").Select(v => Text(v.Value)).ToArray();
                break;
            case "set-body":
                var body = new ApimBody(Encoding.UTF8.GetBytes(Text(node.Value)));
                if (_phase.StartsWith("inbound") || _phase == "backend") Context.Request.Body = body;
                else Context.Response.Body = body;
                break;
            case "set-status":
                Context.Response.StatusCode = int.Parse(Text(node.Attribute("code")!.Value));
                break;
            case "return-response":
                string previous = _phase;
                _phase = "outbound";
                await Children(node);
                _phase = previous;
                throw new PolicyReturn();
            case "authentication-managed-identity":
                Context.Variables[node.Attribute("output-token-variable-name")!.Value] = "offline-identity";
                break;
            case "cache-lookup-value":
            case "cache-store-value":
            case "set-query-parameter":
            case "base":
                // No inherited policy, remote cache or credential query in the fixture.
                break;
            default:
                throw new AssertFailedException($"Unprojected policy instruction: {node.Name}");
        }
    }

    private static Func<string, ApimContext, object> Compile()
    {
        var expressions = Policies.Values.SelectMany(p => p.DescendantsAndSelf())
            .SelectMany(n => n.Attributes().Select(a => a.Value).Concat(n.HasElements ? [] : new[] { n.Value }))
            .Where(v => v.StartsWith("@(") || v.StartsWith("@{")).Distinct().ToArray();
        string scratch = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "ai4ia-policy-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(scratch);
        try
        {
            var source = new StringBuilder("""
                using System;
                using System.Collections.Generic;
                using System.Linq;
                using Newtonsoft.Json;
                using Newtonsoft.Json.Linq;
                using AI4IA.Proxy.Tests;
                public static class ProjectedPolicy {
                public static object Evaluate(string source, ApimContext context) {
                switch (source) {
                """);
            for (int i = 0; i < expressions.Length; i++)
                source.AppendLine($"case {JsonSerializer.Serialize(expressions[i])}: return E{i}(context);");
            source.AppendLine("default: throw new InvalidOperationException(\"Unknown expression\"); } }");
            for (int i = 0; i < expressions.Length; i++)
            {
                string expression = expressions[i];
                string code = expression.StartsWith("@{") ? expression[1..] : "{ return " + expression[2..^1] + "; }";
                source.AppendLine($"private static object E{i}(ApimContext context) {code}");
            }
            source.AppendLine("}");
            string input = System.IO.Path.Combine(scratch, "Policy.cs");
            string output = System.IO.Path.Combine(scratch, "Policy.dll");
            File.WriteAllText(input, source.ToString());
            string root = Directory.GetParent(RuntimeEnvironment.GetRuntimeDirectory().TrimEnd(System.IO.Path.DirectorySeparatorChar))!.Parent!.Parent!.FullName;
            string compiler = Directory.EnumerateDirectories(System.IO.Path.Combine(root, "sdk"))
                .Select(d => System.IO.Path.Combine(d, "Roslyn", "bincore", "csc.dll"))
                .Where(File.Exists).OrderByDescending(p => p, StringComparer.Ordinal).First();
            string[] references = ((string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!).Split(System.IO.Path.PathSeparator)
                .Concat([typeof(ApimContext).Assembly.Location, typeof(JObject).Assembly.Location]).Distinct().ToArray();
            string arguments = System.IO.Path.Combine(scratch, "compile.rsp");
            File.WriteAllLines(arguments, new[] { "-nologo", "-target:library", "-nullable:disable", "-out:\"" + output + "\"" }
                .Concat(references.Select(r => "-reference:\"" + r + "\"")).Append("\"" + input + "\""));
            var start = new ProcessStartInfo("dotnet")
            {
                RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false,
            };
            start.ArgumentList.Add(compiler);
            start.ArgumentList.Add("@" + arguments);
            using var process = Process.Start(start)!;
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(30000))
            {
                process.Kill(entireProcessTree: true);
                throw new AssertFailedException("Policy expression compilation timed out.");
            }
            Assert.AreEqual(0, process.ExitCode, stdout.GetAwaiter().GetResult() + stderr.GetAwaiter().GetResult());
            var assembly = AssemblyLoadContext.Default.LoadFromStream(new MemoryStream(File.ReadAllBytes(output)));
            return assembly.GetType("ProjectedPolicy")!.GetMethod("Evaluate")!
                .CreateDelegate<Func<string, ApimContext, object>>();
        }
        finally { Directory.Delete(scratch, recursive: true); }
    }

    private static string FindPolicies()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            string path = System.IO.Path.Combine(directory.FullName, "infra", "policies");
            if (Directory.Exists(path)) return path;
        }
        throw new AssertFailedException("Repository policy source is unavailable.");
    }

    private static XElement LoadPolicy(string path)
    {
        using var reader = new XmlTextReader(path)
        {
            Normalization = false, DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null,
        };
        return XElement.Load(reader);
    }

    private sealed class PolicyReturn : Exception;
    private sealed class PolicyFault : Exception;
}

public sealed class ApimContext
{
    public Dictionary<string, object> Variables { get; } = new();
    public ApimRequest Request { get; } = new();
    public ApimResponse Response { get; } = new();
    public ApimSubscription? Subscription { get; set; } = new();
    public ApimError LastError { get; } = new();
    public ApimApi Api { get; } = new();
    public Guid RequestId { get; } = Guid.NewGuid();
    public TimeSpan Elapsed => TimeSpan.FromMilliseconds(10);
}
public sealed class ApimApi { public string Id => "fixture"; }
public sealed class ApimSubscription
{
    public string Id => "proxy-models";
    public string PrimaryKey { get; set; } = NoReplayWorkerTests.Key;
    public string SecondaryKey { get; set; } = "";
}
public sealed class ApimError
{
    public string Reason { get; set; } = "";
    public string Message => "offline";
}
public sealed class ApimUrl
{
    public string Path { get; }
    public string QueryString { get; }
    public ApimUrl(string path)
    {
        var parts = path.Split('?', 2);
        Path = parts[0];
        QueryString = parts.Length == 1 ? "" : parts[1];
    }
}
public sealed class ApimRequest
{
    public string Method { get; set; } = "POST";
    public bool HasBody => Body.Bytes.Length > 0;
    public Dictionary<string, string[]> Headers { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, string> MatchedParameters { get; } = new();
    public ApimUrl Url { get; set; } = new("/");
    public ApimUrl OriginalUrl { get; set; } = new("/");
    public ApimBody Body { get; set; } = new([]);
}
public sealed class ApimResponse
{
    public int StatusCode { get; set; } = 500;
    public Dictionary<string, string[]> Headers { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public ApimBody Body { get; set; } = new([]);
}
public sealed class ApimBody(byte[] bytes)
{
    public byte[] Bytes { get; } = bytes;
    public T As<T>(bool preserveContent = false)
    {
        object result = typeof(T) == typeof(byte[]) ? Bytes :
            typeof(T) == typeof(string) ? Encoding.UTF8.GetString(Bytes) :
            JObject.Parse(Encoding.UTF8.GetString(Bytes));
        return (T)result;
    }
}
public static class ApimExtensions
{
    public static T GetValueOrDefault<T>(this Dictionary<string, object> values, string key, T fallback = default!) =>
        values.TryGetValue(key, out var value) ? (T)value : fallback;
    public static string GetValueOrDefault(this Dictionary<string, string[]> headers, string key, string fallback) =>
        headers.TryGetValue(key, out var values) ? string.Join(",", values) : fallback;
}
