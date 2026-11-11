using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.RegularExpressions;
using SimpleL7Proxy.StreamProcessor;

namespace SimpleL7Proxy.Proxy;

// Reduction on the existing deployment route, not an attempts-v1 capability.
internal static class DecisionRequest
{
    internal static bool Matches(string method, string path) =>
        method == "POST" && Regex.IsMatch(path.Split('?')[0],
            @"\A/(openai/)?deployments/[A-Za-z0-9_.-]+/systemone\z");

    internal static bool Matches(RequestData request) => Matches(request.Method, request.Path);

    internal static void RefusePersistence(RequestData request)
    {
        if (Matches(request))
            throw Refused();
    }

    internal static void ValidateState(RequestData request)
    {
        if (Matches(request) && (request.runAsync || request.AsyncTriggered || request.AsyncHydrated ||
            request.IsBackground || request.IsBackgroundCheck || request.IsStatusCheck ||
            request.RecoveryProcessor is not null || request.asyncWorker is not null ||
            request.Requeued || request.LifetimeBackendAttempts != 0))
            throw Refused();
    }

    private static ProxyErrorException Refused() => new(
        ProxyErrorException.ErrorType.InvalidHeader, HttpStatusCode.BadRequest,
        "Decision requests cannot be persisted, requeued or retried.");

    internal static ProxyData Failure(RequestData request, HttpStatusCode status, HttpResponseHeaders? upstream = null)
    {
        var headers = new WebHeaderCollection();
        if (status == HttpStatusCode.TooManyRequests && RetryAfter(upstream) is { } retry)
            headers["Retry-After"] = retry;
        var body = Encoding.UTF8.GetBytes("""{"detail":"Decision request failed; no automatic retry."}""");
        if (request.Context is not null)
        {
            request.Context.Response.StatusCode = (int)status;
            request.Context.Response.Headers = headers;
        }
        return new ProxyData
        {
            StatusCode = status,
            Headers = headers,
            Body = body,
            BodyResponseMessage = new HttpResponseMessage(status) { Content = new ByteArrayContent(body) },
            StreamingProcessor = StreamProcessorFactory.DEFAULT_PROCESSOR,
            ContentHeaders = new WebHeaderCollection
            {
                ["Content-Type"] = "application/json; charset=utf-8",
                ["Content-Length"] = body.Length.ToString(CultureInfo.InvariantCulture)
            }
        };
    }

    private static string? RetryAfter(HttpResponseHeaders? headers)
    {
        if (headers is null || !headers.TryGetValues("Retry-After", out var values))
            return null;
        var items = values.Take(2).ToArray();
        if (items.Length != 1 || items[0].Length > 128)
            return null;
        var raw = items[0];
        double delay;
        if (raw.Length > 0 && raw.All(c => c is >= '0' and <= '9') &&
            int.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out int seconds))
            delay = seconds;
        else if (DateTimeOffset.TryParseExact(raw, "r", CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal, out var date))
            delay = (date - DateTimeOffset.UtcNow).TotalSeconds;
        else
            return null;
        return delay is >= 0 and <= 86400
            ? Math.Ceiling(delay).ToString(CultureInfo.InvariantCulture) : null;
    }
}
