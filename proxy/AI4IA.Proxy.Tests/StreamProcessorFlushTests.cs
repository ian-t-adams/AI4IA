using System.Net.Http;
using System.Text;
using Microsoft.VisualStudio.TestTools.UnitTesting;
using SimpleL7Proxy.StreamProcessor;

namespace AI4IA.Proxy.Tests;

/// <summary>
/// Pins that a streamed response actually reaches the caller incrementally.
///
/// APIM sets the <c>TOKENPROCESSOR</c> header for <c>application/json</c> and
/// <c>text/*</c> responses (infra/policies/simplel7proxy-priority-retry.xml), so
/// every streaming chat completion is re-emitted through
/// <see cref="JsonStreamProcessor"/> rather than passed through byte-for-byte.
///
/// That processor wraps the output in a <c>StreamWriter</c> with a 4 KiB buffer
/// and never flushed it per line; the proxy's periodic StreamFlusher flushes the
/// UNDERLYING stream, which cannot see characters still sitting in the writer.
/// The result was that a token-by-token SSE response was withheld until ~4 KiB
/// had accumulated (or the response ended) -- the user sees a long stall and
/// then a burst, which for a short answer means no streaming at all.
///
/// The assertion is deliberately about *arrival before completion* rather than
/// wall-clock timing, so it is deterministic in CI.
/// </summary>
[TestClass]
public class StreamProcessorFlushTests
{
    /// One SSE event, comfortably under the writer's 4 KiB buffer so that an
    /// unflushed writer would deliver nothing at all until disposal.
    private const string Event =
        "data: {\"choices\":[{\"delta\":{\"content\":\"token\"}}]}";

    /// <summary>Records the output as the processor produces it.</summary>
    private sealed class RecordingStream : Stream
    {
        private readonly MemoryStream _inner = new();
        public int WriteOperations { get; private set; }
        public long BytesAtFirstWrite { get; private set; } = -1;

        private void Record(int count)
        {
            WriteOperations++;
            if (BytesAtFirstWrite < 0) BytesAtFirstWrite = count;
        }

        public override void Write(byte[] buffer, int offset, int count)
        {
            Record(count);
            _inner.Write(buffer, offset, count);
        }

        public override ValueTask WriteAsync(
            ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
        {
            Record(buffer.Length);
            return _inner.WriteAsync(buffer, cancellationToken);
        }

        public override Task WriteAsync(
            byte[] buffer, int offset, int count, CancellationToken cancellationToken)
        {
            Record(count);
            return _inner.WriteAsync(buffer, offset, count, cancellationToken);
        }

        public string Text => Encoding.UTF8.GetString(_inner.ToArray());

        public override bool CanRead => false;
        public override bool CanSeek => false;
        public override bool CanWrite => true;
        public override long Length => _inner.Length;
        public override long Position { get => _inner.Position; set => throw new NotSupportedException(); }
        public override void Flush() => _inner.Flush();
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
    }

    private static HttpContent SseContent(int events)
    {
        var body = string.Join("\n", Enumerable.Repeat(Event, events).Append("data: [DONE]"));
        var content = new StringContent(body, Encoding.UTF8);
        content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("text/event-stream");
        return content;
    }

    [TestMethod]
    public async Task EachEventIsFlushedRatherThanBufferedUntilTheEnd()
    {
        var output = new RecordingStream();
        // MultiLineAllUsageProcessor is the processor APIM actually names.
        var processor = new MultiLineAllUsageProcessor();

        await processor.CopyToAsync(SseContent(events: 6), output);

        // 6 events + the [DONE] line. Without a per-line flush the whole body
        // (well under 4 KiB) left the writer in ONE write at disposal.
        Assert.IsTrue(
            output.WriteOperations >= 7,
            $"expected at least one write per SSE event, saw {output.WriteOperations} " +
            "-- the response is being buffered instead of streamed");
    }

    [TestMethod]
    public async Task FirstEventIsNotWithheldBehindLaterOnes()
    {
        var output = new RecordingStream();
        var processor = new MultiLineAllUsageProcessor();

        await processor.CopyToAsync(SseContent(events: 6), output);

        // The first write must carry roughly one event, not the whole response:
        // that is the difference between first-token latency and all-at-once.
        Assert.IsTrue(
            output.BytesAtFirstWrite > 0 && output.BytesAtFirstWrite < Event.Length * 3,
            $"first write carried {output.BytesAtFirstWrite} bytes; expected about one " +
            $"event ({Event.Length}), which means earlier events waited for later ones");
    }

    [TestMethod]
    public async Task BodyIsStillForwardedIntact()
    {
        // Flushing must not change what the caller receives.
        var output = new RecordingStream();
        var processor = new MultiLineAllUsageProcessor();

        await processor.CopyToAsync(SseContent(events: 3), output);

        var lines = output.Text.Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Select(l => l.TrimEnd('\r'))
            .ToArray();
        Assert.AreEqual(4, lines.Length);
        Assert.IsTrue(lines.Take(3).All(l => l == Event));
        Assert.AreEqual("data: [DONE]", lines[^1]);
    }
}
