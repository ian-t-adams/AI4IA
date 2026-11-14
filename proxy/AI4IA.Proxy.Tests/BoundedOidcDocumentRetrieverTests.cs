using System.Net;
using System.Text;
using SimpleL7Proxy.Config;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class BoundedOidcDocumentRetrieverTests
{
    private const string MetadataUrl =
        "https://login.microsoftonline.com/11111111-2222-4333-8444-555555555555/v2.0/.well-known/openid-configuration";

    [TestMethod]
    public async Task RetrievesOnlyTheTrustedHttpsHost()
    {
        var handler = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("{\"issuer\":\"trusted\"}", Encoding.UTF8, "application/json")
        });
        using var client = new HttpClient(handler);
        var retriever = new BoundedOidcDocumentRetriever(client);

        var document = await retriever.GetDocumentAsync(MetadataUrl, CancellationToken.None);

        Assert.AreEqual("{\"issuer\":\"trusted\"}", document);
        Assert.AreEqual(MetadataUrl, handler.LastRequestUri?.AbsoluteUri);
    }

    [TestMethod]
    public async Task RefusesUntrustedUrlWithoutSendingRequest()
    {
        var handler = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.OK));
        using var client = new HttpClient(handler);
        var retriever = new BoundedOidcDocumentRetriever(client);

        await Assert.ThrowsExceptionAsync<HttpRequestException>(
            () => retriever.GetDocumentAsync("https://example.com/metadata", CancellationToken.None));

        Assert.IsNull(handler.LastRequestUri);
    }

    [TestMethod]
    public async Task RefusesRedirectAndOversizedMetadata()
    {
        var redirectHandler = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.Redirect));
        using var redirectClient = new HttpClient(redirectHandler);
        var redirectRetriever = new BoundedOidcDocumentRetriever(redirectClient);
        await Assert.ThrowsExceptionAsync<HttpRequestException>(
            () => redirectRetriever.GetDocumentAsync(MetadataUrl, CancellationToken.None));
        Assert.AreEqual(MetadataUrl, redirectHandler.LastRequestUri?.AbsoluteUri);

        var largeHandler = new StubHandler(_ =>
        {
            var response = new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new ByteArrayContent(new byte[1024 * 1024 + 1])
            };
            response.Content.Headers.ContentLength = 1024 * 1024 + 1;
            return response;
        });
        using var largeClient = new HttpClient(largeHandler);
        var largeRetriever = new BoundedOidcDocumentRetriever(largeClient);
        await Assert.ThrowsExceptionAsync<HttpRequestException>(
            () => largeRetriever.GetDocumentAsync(MetadataUrl, CancellationToken.None));
    }

    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public Uri? LastRequestUri { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            LastRequestUri = request.RequestUri;
            return Task.FromResult(respond(request));
        }
    }
}
