using System.Net;
using System.Text;
using Microsoft.IdentityModel.Protocols;

namespace SimpleL7Proxy.Config;

internal sealed class BoundedOidcDocumentRetriever : IDocumentRetriever
{
    private const int MaxDocumentBytes = 1024 * 1024;
    private static readonly TimeSpan s_timeout = TimeSpan.FromSeconds(5);
    private static readonly HttpClient s_client = new(new HttpClientHandler
    {
        AllowAutoRedirect = false,
        UseCookies = false,
        Credentials = null
    })
    {
        Timeout = Timeout.InfiniteTimeSpan
    };
    private readonly HttpClient _client;

    public BoundedOidcDocumentRetriever() : this(s_client)
    {
    }

    internal BoundedOidcDocumentRetriever(HttpClient client)
    {
        _client = client ?? throw new ArgumentNullException(nameof(client));
    }

    public async Task<string> GetDocumentAsync(string address, CancellationToken cancellationToken)
    {
        if (!Uri.TryCreate(address, UriKind.Absolute, out var uri) ||
            uri.Scheme != Uri.UriSchemeHttps ||
            !uri.IsDefaultPort ||
            !string.Equals(uri.Host, "login.microsoftonline.com", StringComparison.OrdinalIgnoreCase) ||
            !string.IsNullOrEmpty(uri.UserInfo) ||
            !string.IsNullOrEmpty(uri.Query) ||
            !string.IsNullOrEmpty(uri.Fragment))
            throw new HttpRequestException("OIDC metadata URL is outside the trusted HTTPS host.");

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(s_timeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        using var response = await _client.SendAsync(
            request, HttpCompletionOption.ResponseHeadersRead, timeout.Token).ConfigureAwait(false);
        if (response.StatusCode != HttpStatusCode.OK)
            throw new HttpRequestException("OIDC metadata request failed.");
        if (response.Content.Headers.ContentLength > MaxDocumentBytes)
            throw new HttpRequestException("OIDC metadata document exceeds the size limit.");

        await using var stream = await response.Content.ReadAsStreamAsync(timeout.Token).ConfigureAwait(false);
        using var buffer = new MemoryStream();
        var chunk = new byte[8192];
        while (true)
        {
            var read = await stream.ReadAsync(chunk, timeout.Token).ConfigureAwait(false);
            if (read == 0)
                break;
            if (buffer.Length + read > MaxDocumentBytes)
                throw new HttpRequestException("OIDC metadata document exceeds the size limit.");
            buffer.Write(chunk, 0, read);
        }

        return new UTF8Encoding(false, true).GetString(buffer.ToArray());
    }
}
