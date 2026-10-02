using System.Runtime.CompilerServices;
using System.Xml;
using System.Xml.Linq;

namespace AI4IA.Proxy.Tests;

// Compiles the actual generated infra/policies/speech-voice-live.xml expressions with the
// SDK compiler, through the offline APIM projection, and evaluates its onHandshake decisions
// against projected query strings: refusals in order, then the api-version and model values.
// Not Azure's policy engine, its WebSocket handshake or live evidence. It proves the
// expressions compile and pick the pinned version, the opt-in Live-Reference AEC pair and
// the refusals exactly as generated.
[TestClass]
[DoNotParallelize]
public sealed class SpeechVoiceLiveApimTests
{
    private const string Pinned = "2026-04-10";
    private const string EchoVersion = "2026-07-15";
    private const string EchoFlag = "client_ec_reference:true";
    private const string FeatureRefusal = "Voice Live feature is not in the AI4IA catalog";
    private const string ModelRefusal = "Voice Live model is not in the AI4IA catalog";

    private static XElement _policy = null!;
    private static Func<string, ApimContext, object> _evaluate = null!;

    [ClassInitialize]
    public static void Initialize(TestContext _)
    {
        string path = Path.GetFullPath(Path.Combine(
            Path.GetDirectoryName(SourceFile())!, "..", "..", "infra", "policies", "speech-voice-live.xml"));
        using var reader = new XmlTextReader(path)
        {
            Normalization = false, DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null,
        };
        _policy = XElement.Load(reader);
        _evaluate = ApimPolicyHarness.CompilePolicies([_policy]);
    }

    private static string SourceFile([CallerFilePath] string path = "") => path;

    private sealed record Decision(string? Refusal, string? ApiVersion, string? Model);

    // The inbound decision for one handshake: the first matching refusal, otherwise the
    // forwarded api-version and model. Every other query parameter passes through unchanged.
    private static Decision Decide(string query)
    {
        var context = new ApimContext();
        context.Request.Url = new ApimUrl("/speech/voice-live/realtime" + query);
        context.Request.OriginalUrl = context.Request.Url;
        var inbound = _policy.Element("inbound")!;
        foreach (var when in inbound.Element("choose")!.Elements("when"))
        {
            if ((bool)_evaluate(when.Attribute("condition")!.Value, context))
                return new Decision(when.Descendants("set-status").Single().Attribute("reason")!.Value, null, null);
        }
        string Value(string name) => (string)_evaluate(
            inbound.Elements("set-query-parameter").Single(e => e.Attribute("name")!.Value == name)
                .Element("value")!.Value,
            context);
        return new Decision(null, Value("api-version"), Value("model"));
    }

    [TestMethod]
    public void EverySessionThatDoesNotOptInStaysOnThePinnedVersion()
    {
        Assert.AreEqual(new Decision(null, Pinned, "gpt-realtime"), Decide($"?api-version={Pinned}&model=gpt-realtime"));
        // The echo-reference version alone is not an opt-in.
        Assert.AreEqual(new Decision(null, Pinned, "gpt-4.1"), Decide($"?api-version={EchoVersion}&model=gpt-4.1"));
        Assert.AreEqual(new Decision(null, Pinned, "gpt-realtime"), Decide("?api-version=2099-01-01"));
        Assert.AreEqual(new Decision(null, Pinned, "gpt-realtime"), Decide("?model="));
    }

    [TestMethod]
    public void OnlyTheExactEchoReferencePairLeavesThePinnedVersion()
    {
        Assert.AreEqual(
            new Decision(null, EchoVersion, "gpt-realtime"),
            Decide($"?api-version={EchoVersion}&model=gpt-realtime&features={EchoFlag}"));
        // The projection decodes the query, as APIM's parsed query does.
        Assert.AreEqual(
            new Decision(null, EchoVersion, "gpt-5.1"),
            Decide($"?api-version={EchoVersion}&model=gpt-5.1&features=client_ec_reference%3Atrue"));
    }

    [TestMethod]
    [DataRow("?api-version=2026-04-10&model=gpt-realtime&features=client_ec_reference:true")]
    [DataRow("?model=gpt-realtime&features=client_ec_reference:true")]
    [DataRow("?api-version=2026-07-15&model=gpt-realtime&features=client_ec_reference:false")]
    [DataRow("?api-version=2026-07-15&model=gpt-realtime&features=CLIENT_EC_REFERENCE:true")]
    [DataRow("?api-version=2026-07-15&model=gpt-realtime&features=other_flag:true")]
    [DataRow("?api-version=2026-07-15&model=gpt-realtime&features=")]
    [DataRow("?api-version=2026-07-15&model=gpt-realtime&features=client_ec_reference:true&features=client_ec_reference:true")]
    public void AnyOtherFeaturesValueIsRefused(string query)
    {
        Assert.AreEqual(new Decision(FeatureRefusal, null, null), Decide(query));
        // Control: the same handshake without the parameter connects at the pinned version.
        string without = string.Join('&', query.TrimStart('?').Split('&').Where(p => !p.StartsWith("features=")));
        Assert.AreEqual(Pinned, Decide("?" + without).ApiVersion);
    }

    [TestMethod]
    public void AnUnknownModelIsRefusedBeforeAnyFeature()
    {
        Assert.AreEqual(
            new Decision(ModelRefusal, null, null),
            Decide($"?api-version={EchoVersion}&model=attacker&features={EchoFlag}"));
        Assert.AreEqual(new Decision(ModelRefusal, null, null), Decide("?model=GPT-REALTIME"));
    }
}
