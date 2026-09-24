using System.Text;

namespace AI4IA.Proxy.Tests;

// Drives the actual vendored ProxyWorker with a multipart image-edit body, the
// shape the API sends for images/edits and audio transcription. Loopback hosts
// stand in for APIM; this is not live proxy, APIM or provider evidence.
[TestClass]
[DoNotParallelize] // The vendored worker's queue/header configuration is static.
public sealed class MultipartProxyWorkerTests
{
    private const string Boundary = "ai4ia-worker-fixture-boundary";
    private const string Deployment = "gpt-image-2-fixture-eastus2-glbl";
    private const string EditPath = $"/openai/deployments/{Deployment}/images/edits?api-version=2025-04-01-preview";
    private static readonly byte[] Multipart = Build();

    // Bytes that are not JSON and not valid UTF-8, including a boundary-looking
    // line inside the file part, so any parse or re-encoding would change them.
    private static byte[] Build()
    {
        using var stream = new MemoryStream();
        void Text(string value) => stream.Write(Encoding.ASCII.GetBytes(value));
        Text($"--{Boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nadd a moon\r\n");
        Text($"--{Boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"image.png\"\r\n");
        Text("Content-Type: image/png\r\n\r\n");
        stream.Write([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0xFF, 0x7B, 0x22, 0xC3, 0x28]);
        Text($"\r\n--not-the-boundary\r\n\r\n--{Boundary}--\r\n");
        return stream.ToArray();
    }

    [TestMethod]
    public async Task MultipartEditBytesCrossTheActualWorkerUnchanged()
    {
        await using var host = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var fixture = await NoReplayWorkerTests.WorkerFixture.Create(
            [host], bounded: false, path: EditPath, body: Multipart,
            contentType: $"multipart/form-data; boundary={Boundary}");
        using var result = await fixture.Send();
        Assert.AreEqual(200, (int)result.StatusCode);
        var sent = host.Requests.Single();
        Assert.AreEqual(EditPath, sent.Path);
        CollectionAssert.AreEqual(Multipart, sent.Body);
        // The boundary survives. The vendored worker appends a charset parameter
        // when none is present (the same as for transcription); a live multipart
        // parser must ignore it, which remains a post-deploy verification item.
        string forwarded = sent.Headers["Content-Type"];
        StringAssert.StartsWith(forwarded, "multipart/form-data");
        StringAssert.Contains(forwarded, $"boundary={Boundary}");
        // The model header comes from the deployment path; the JSON body sniff
        // fails harmlessly on multipart instead of replacing it.
        Assert.AreEqual(Deployment, sent.Headers["x-LLMModel"]);
    }

    [TestMethod]
    public async Task LegacyFailoverResendsTheSameMultipartBytes()
    {
        await using var second = new WireServer(_ => Task.FromResult(new WireReply(200)));
        await using var first = new WireServer(_ => Task.FromResult(new WireReply(429)));
        await using var fixture = await NoReplayWorkerTests.WorkerFixture.Create(
            [first, second], bounded: false, path: EditPath, body: Multipart,
            contentType: $"multipart/form-data; boundary={Boundary}");
        using var result = await fixture.Send();
        Assert.AreEqual(200, (int)result.StatusCode);
        CollectionAssert.AreEqual(Multipart, first.Requests.Single().Body);
        CollectionAssert.AreEqual(Multipart, second.Requests.Single().Body);
    }
}
