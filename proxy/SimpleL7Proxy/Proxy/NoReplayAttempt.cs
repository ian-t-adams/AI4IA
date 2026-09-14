using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using SimpleL7Proxy.Backend;
using SimpleL7Proxy.Config;

namespace SimpleL7Proxy.Proxy;

/// <summary>Authenticated reduction only. No durable recovery or retry authority.</summary>
public sealed class NoReplayAttempt
{
    public const string Version = "ai4ia-one-attempt-v1";
    public const string RequestHeader = "x-ai4ia-attempt";
    public const string ProofHeader = "x-ai4ia-proxy-attempt";
    public const string AckHeader = "x-ai4ia-attempt-ack";
    public const int MaxBodyBytes = 1024 * 1024;
    private readonly string _nonce;
    private readonly string _bodyHash;
    private readonly string _ingressPath;
    private int _claimed;

    private NoReplayAttempt(string nonce, string bodyHash, string path)
    {
        _nonce = nonce;
        _bodyHash = bodyHash;
        _ingressPath = path;
    }

    public bool Claimed => Volatile.Read(ref _claimed) != 0;

    public static bool IsInternalHeader(string name) =>
        name.StartsWith("x-ai4ia-attempt", StringComparison.OrdinalIgnoreCase) ||
        name.StartsWith("x-ai4ia-proxy-attempt", StringComparison.OrdinalIgnoreCase);

    public static void BindAuthenticated(RequestData request, bool authenticatedKey, ProxyConfig options)
    {
        var names = request.Headers.AllKeys.Where(k => k is not null && IsInternalHeader(k)).ToArray();
        if (names.Length == 0)
            return;
        if (names.Length != 1 || !string.Equals(names[0], RequestHeader, StringComparison.OrdinalIgnoreCase))
            throw Refused("Untrusted gateway attempt metadata.");
        var raw = request.Headers.GetValues(RequestHeader);
        if (!authenticatedKey || raw is not { Length: 1 })
            throw Refused("Gateway attempt requires authenticated proxy ingress.");
        var match = Regex.Match(raw[0], @"\Aai4ia-one-attempt-v1\.([0-9a-f]{32})\.([0-9a-f]{64})\z");
        if (!match.Success || request.NoReplay is not null)
            throw Refused("Invalid gateway attempt contract.");
        request.NoReplay = new NoReplayAttempt(match.Groups[1].Value, match.Groups[2].Value, request.Path);
        ValidateState(request);
        if (request.Headers[options.AsyncClientRequestHeader] is not null ||
            request.Headers["S7PType"] is not null || request.Headers["Guid"] is not null ||
            request.Headers["S7PDEBUG"] is not null || options.IgnoreSSLCert)
            throw Refused("Unsupported bounded gateway request.");
        // Never forward an ingress claim as a proxy attestation.
        request.Headers.Remove(RequestHeader);
    }

    public static void ValidateState(RequestData request)
    {
        if (request.NoReplay is null)
        {
            if (request.Headers.AllKeys.Any(k => k is not null && IsInternalHeader(k)))
                throw Refused("Gateway attempt was not authenticated.");
            return;
        }
        if (request.runAsync || request.AsyncTriggered || request.AsyncHydrated ||
            request.IsBackground || request.IsBackgroundCheck || request.IsStatusCheck ||
            request.RecoveryProcessor is not null || request.asyncWorker is not null ||
            request.Requeued || request.NoReplay.Claimed)
            throw Refused("Gateway attempt cannot be retried or recovered.");
    }

    public static void RefusePersistence(RequestData request)
    {
        if (request.NoReplay is not null || request.Headers.AllKeys.Any(k => k is not null && IsInternalHeader(k)))
            throw Refused("Gateway attempt cannot be persisted or requeued.");
    }

    public void Claim(RequestData request, HttpRequestMessage outgoing, byte[] body, HostConfig host)
    {
        ValidateState(request);
        if (request.Path != _ingressPath || request.Method != "POST" || body.Length > MaxBodyBytes ||
            Convert.ToHexStringLower(SHA256.HashData(body)) != _bodyHash ||
            host.DirectMode || host.AuthMode != AuthModeEnum.ApiKey ||
            !string.Equals(host.ApiKeyHeader, "Ocp-Apim-Subscription-Key", StringComparison.OrdinalIgnoreCase) ||
            string.IsNullOrEmpty(host.ApiKey) || outgoing.RequestUri is null ||
            outgoing.RequestUri.PathAndQuery != _ingressPath ||
            (outgoing.RequestUri.Scheme != "https" && !outgoing.RequestUri.IsLoopback))
            throw Refused("Gateway attempt binding does not match.");
        ValidatePayload(body, request);
        outgoing.Headers.Remove("x-LLMModel");
        outgoing.Headers.Add("x-LLMModel", request.Model);
        if (Interlocked.CompareExchange(ref _claimed, 1, 0) != 0)
            throw Refused("Gateway attempt was already dispatched.");

        foreach (var name in outgoing.Headers.Select(pair => pair.Key).Where(IsInternalHeader).ToArray())
            outgoing.Headers.Remove(name);
        string value = $"{Version}.{_nonce}.{_bodyHash}";
        string signed = $"{value}\nPOST\n{outgoing.RequestUri.PathAndQuery}\n{request.Model}";
        string signature = Convert.ToHexStringLower(HMACSHA256.HashData(
            Encoding.UTF8.GetBytes(host.ApiKey), Encoding.UTF8.GetBytes(signed)));
        outgoing.Headers.Add(RequestHeader, value);
        outgoing.Headers.Add(ProofHeader, signature);
        outgoing.Version = HttpVersion.Version11;
        outgoing.VersionPolicy = HttpVersionPolicy.RequestVersionExact;
        outgoing.Headers.ExpectContinue = false;
        outgoing.Headers.ConnectionClose = true;
    }

    public void CheckResponse(HttpResponseMessage response)
    {
        if (!response.Headers.TryGetValues(AckHeader, out var values) ||
            !values.SequenceEqual(new[] { $"{Version}.{_nonce}" }))
            throw Refused("Gateway attempt acknowledgement is unavailable.", HttpStatusCode.BadGateway);
        response.Headers.Remove("S7PREQUEUE");
    }

    public static HttpClient CreateClient() => new(new SocketsHttpHandler
    {
        AllowAutoRedirect = false,
        UseCookies = false,
        UseProxy = false,
        Credentials = null,
        PreAuthenticate = false,
        PooledConnectionLifetime = TimeSpan.Zero,
        PooledConnectionIdleTimeout = TimeSpan.Zero,
    })
    {
        Timeout = Timeout.InfiniteTimeSpan,
        DefaultRequestVersion = HttpVersion.Version11,
        DefaultVersionPolicy = HttpVersionPolicy.RequestVersionExact,
    };

    private static void ValidatePayload(byte[] body, RequestData request)
    {
        try
        {
            using var document = JsonDocument.Parse(body, new JsonDocumentOptions { MaxDepth = 64 });
            var root = document.RootElement;
            string path = request.Path.Split('?')[0];
            bool embedding = Regex.IsMatch(path, @"/deployments/[A-Za-z0-9_.-]+/embeddings\z");
            bool chat = Regex.IsMatch(path, @"/deployments/[A-Za-z0-9_.-]+/chat/completions\z");
            bool responses = path.EndsWith("/responses", StringComparison.Ordinal);
            if (root.ValueKind != JsonValueKind.Object || (!embedding && !chat && !responses))
                throw Refused("Unsupported bounded gateway operation.");
            if (root.TryGetProperty("model", out var model))
            {
                if (model.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(model.GetString()))
                    throw Refused("Invalid bounded gateway model.");
                request.Model = model.GetString()!;
            }
            if (string.IsNullOrEmpty(request.Model))
                throw Refused("Invalid bounded gateway model.");
            var pathModel = Regex.Match(path, @"/deployments/([A-Za-z0-9_.-]+)/");
            if (pathModel.Success && pathModel.Groups[1].Value != request.Model)
                throw Refused("Bounded gateway model and path disagree.");
            string[] keys = embedding ? ["model", "input"] :
                ["model", "messages", "input", "instructions", "system", "stream", "stream_options",
                 "max_tokens", "max_completion_tokens", "max_output_tokens", "temperature", "top_p",
                 "top_k", "presence_penalty", "frequency_penalty", "seed", "stop", "stop_sequences",
                 "reasoning", "reasoning_effort", "text", "response_format", "store", "n"];
            var properties = root.EnumerateObject().ToArray();
            if (properties.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != properties.Length ||
                properties.Any(p => !keys.Contains(p.Name, StringComparer.Ordinal)))
                throw Refused("Unsupported bounded gateway payload.");
            if (embedding)
            {
                if (!root.TryGetProperty("input", out var inputs) || inputs.ValueKind != JsonValueKind.Array ||
                    inputs.GetArrayLength() == 0 || inputs.EnumerateArray().Any(v => v.ValueKind != JsonValueKind.String))
                    throw Refused("Unsupported bounded embedding payload.");
                return;
            }
            if ((root.TryGetProperty("store", out var store) && store.ValueKind != JsonValueKind.False) ||
                (root.TryGetProperty("n", out var n) && (!n.TryGetInt32(out int count) || count != 1)))
                throw Refused("Unsupported bounded gateway payload.");
            var messages = root.TryGetProperty("messages", out var items) ? items : root.GetProperty("input");
            if (messages.ValueKind != JsonValueKind.Array || messages.GetArrayLength() == 0)
                throw Refused("Unsupported bounded gateway messages.");
            foreach (var message in messages.EnumerateArray())
            {
                if (message.ValueKind != JsonValueKind.Object ||
                    message.EnumerateObject().Any(p => !new[] { "role", "content", "type", "name" }.Contains(p.Name)) ||
                    !message.TryGetProperty("role", out var role) ||
                    role.GetString() is not ("system" or "developer" or "user" or "assistant") ||
                    (message.TryGetProperty("type", out var type) && type.GetString() != "message") ||
                    !PlainText(message.GetProperty("content")))
                    throw Refused("Unsupported bounded gateway messages.");
            }
            if (root.TryGetProperty("system", out var system) && !PlainText(system))
                throw Refused("Unsupported bounded gateway system content.");
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or KeyNotFoundException)
        {
            throw Refused("Unsupported bounded gateway payload.");
        }
    }

    private static bool PlainText(JsonElement content) =>
        content.ValueKind == JsonValueKind.String ||
        (content.ValueKind == JsonValueKind.Array && content.GetArrayLength() > 0 &&
         content.EnumerateArray().All(block =>
             block.ValueKind == JsonValueKind.Object && block.EnumerateObject().Count() == 2 &&
             block.TryGetProperty("type", out var kind) && kind.GetString() is "text" or "input_text" or "output_text" &&
             block.TryGetProperty("text", out var text) && text.ValueKind == JsonValueKind.String));

    private static ProxyErrorException Refused(string message, HttpStatusCode code = HttpStatusCode.BadRequest) =>
        new(ProxyErrorException.ErrorType.InvalidHeader, code, message);
}
