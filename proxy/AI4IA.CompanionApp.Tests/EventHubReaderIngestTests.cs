using System.Collections.Concurrent;
using System.Reflection;
using System.Runtime.CompilerServices;
using Azure.Messaging.EventHubs;
using Azure.Messaging.EventHubs.Consumer;
using CompanionApp.Components.Shared;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;

namespace AI4IA.CompanionApp.Tests;

/// <summary>
/// Drives the real <see cref="EventHubReader"/> partition fan-out (ConsumeAsync → one reader loop
/// per partition → the pipeline) with a fake consumer client, so the serialization and the
/// no-persistence guarantees are proven on the code path production runs.
/// </summary>
[TestClass]
public sealed class EventHubReaderIngestTests
{
    private const int Partitions = 4;
    // Sized for the pipeline's per-event metrics recompute, which grows with the retained records.
    private const int RequestsPerPartition = 250;

    [DataTestMethod]
    [DataRow(true, DisplayName = "four partitions read concurrently")]
    [DataRow(false, DisplayName = "control: the same events on one partition")]
    public async Task EveryRequestIsFinalizedExactlyOnceWithNoLeakedState(bool concurrent)
    {
        var workloads = Enumerable.Range(0, Partitions).Select(p => Workload($"p{p}", RequestsPerPartition)).ToArray();
        var partitions = concurrent
            ? workloads.Select((events, p) => (Id: p.ToString(), Events: events)).ToDictionary(x => x.Id, x => x.Events)
            : new Dictionary<string, string[]> { ["0"] = workloads.SelectMany(events => events).ToArray() };

        var harness = new ReaderHarness();
        var consumer = new FakeConsumer(partitions);
        await harness.ConsumeAsync(consumer);

        // Non-vacuity: the concurrent row really overlapped its partition loops.
        Assert.AreEqual(concurrent ? Partitions : 1, consumer.MaxActivePartitions);
        Assert.AreEqual(0, harness.Logger.Problems.Count, string.Join(Environment.NewLine, harness.Logger.Problems.Take(3)));
        var requests = harness.Store.GetSnapshot().Requests;
        Assert.AreEqual(Partitions * RequestsPerPartition, requests.Count, "one row per request, finalized in place");
        Assert.IsTrue(requests.All(item => item.IsComplete && !item.IsRunning && item.StatusCode == 200));
        Assert.AreEqual(0, harness.PrivateCount("_requestPhases"), "leaked request phases");
        Assert.AreEqual(0, harness.PrivateCount("_requestLifecycle"), "leaked request lifecycles");
    }

    [TestMethod]
    public async Task UnlabeledBackendAttemptsAreProcessedButNothingIsPersisted()
    {
        // Upstream appended exactly these events (no x-backend-label, no "Using <NAME> URL:")
        // to an unbounded incomplete.json beside the binary, carrying user id, path and host.
        const string attempt = """{"Type":"S7P-BackendRequest","S7P-ID":"u1","GUID":"g-u1","MID":"u1-1","Status":"401","UserID":"fixture-user","Path":"/openai/responses","Backend-Host":"apim.fixture.invalid","backendLog":"no label"}""";
        const string final = """{"Type":"S7P-ProxyRequest","S7P-ID":"u1","GUID":"g-u1","Status":"401","UserID":"fixture-user","Path":"/openai/responses","Backend-Host":"apim.fixture.invalid"}""";
        var before = Listing(AppContext.BaseDirectory);

        var harness = new ReaderHarness();
        await harness.ConsumeAsync(new FakeConsumer(new Dictionary<string, string[]> { ["0"] = [attempt, final] }));

        // Control: both events reached the pipeline and produced their rows.
        var requests = harness.Store.GetSnapshot().Requests;
        CollectionAssert.AreEquivalent(new[] { "S7P-BackendRequest", "S7P-ProxyRequest" }, requests.Select(item => item.EventType).ToArray());
        Assert.IsFalse(File.Exists(Path.Combine(AppContext.BaseDirectory, "incomplete.json")));
        CollectionAssert.AreEqual(before, Listing(AppContext.BaseDirectory), "the reader wrote beside the binary");
    }

    private static string[] Workload(string prefix, int count)
    {
        var events = new string[count * 2];
        for (int i = 0; i < count; i++)
        {
            string id = $"{prefix}-{i}";
            events[2 * i] = $$"""{"Type":"S7P-ProxyRequestEnqueued","S7P-ID":"{{id}}","GUID":"g-{{id}}","Date":"2026-09-26T00:00:00Z","Path":"/openai/responses","UserID":"fixture","QueueLength":"1","ActiveHosts":"1"}""";
            events[2 * i + 1] = $$"""{"Type":"S7P-ProxyRequest","S7P-ID":"{{id}}","GUID":"g-{{id}}","Date":"2026-09-26T00:00:01Z","Status":"200","Path":"/openai/responses","UserID":"fixture","Backend-Host":"apim.fixture.invalid"}""";
        }
        return events;
    }

    private static string[] Listing(string directory) =>
        Directory.EnumerateFileSystemEntries(directory, "*", SearchOption.AllDirectories)
            .Where(path => !path.Contains(Path.DirectorySeparatorChar + "TestResults" + Path.DirectorySeparatorChar))
            .Select(path => $"{Path.GetRelativePath(directory, path)}|{(File.Exists(path) ? new FileInfo(path).Length : -1)}")
            .Order(StringComparer.Ordinal)
            .ToArray();

    private sealed class ReaderHarness
    {
        private static readonly MethodInfo Consume = typeof(EventHubReader)
            .GetMethod("ConsumeAsync", BindingFlags.NonPublic | BindingFlags.Instance)!;
        private static readonly Type SettingsType = typeof(EventHubReader)
            .GetNestedType("ReaderSettings", BindingFlags.NonPublic)!;

        internal EventHubMonitorStore Store { get; } = new();
        internal RecordingLogger Logger { get; } = new();
        private readonly EventHubReader _reader;

        internal ReaderHarness() =>
            _reader = new EventHubReader(Store, new ProxyMetricsCatalog(), Options.Create(new EventHubMonitorOptions()), Logger);

        internal async Task ConsumeAsync(EventHubConsumerClient consumer)
        {
            var settings = SettingsType.GetConstructors().Single().Invoke(
                [true, null, null, "telemetry", "companion", "fixture.servicebus.windows.net", "earliest"]);
            using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(60));
            // Bounded: a corrupted dictionary can spin forever instead of throwing.
            await ((Task)Consume.Invoke(_reader, [consumer, settings, stop.Token])!).WaitAsync(TimeSpan.FromSeconds(60));
        }

        internal int PrivateCount(string field)
        {
            var value = typeof(EventHubReader).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(_reader)!;
            return (int)value.GetType().GetProperty("Count")!.GetValue(value)!;
        }
    }

    /// <summary>Delivers each partition's events once, overlapping the partition loops.</summary>
    private sealed class FakeConsumer(IReadOnlyDictionary<string, string[]> partitions) : EventHubConsumerClient
    {
        private readonly TaskCompletionSource _allStarted = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly ConcurrentDictionary<string, int> _reads = new();
        private int _started;
        private int _active;
        private int _maxActive;

        internal int MaxActivePartitions => Volatile.Read(ref _maxActive);

        public override Task<string[]> GetPartitionIdsAsync(CancellationToken cancellationToken = default) =>
            Task.FromResult(partitions.Keys.ToArray());

        public override async IAsyncEnumerable<PartitionEvent> ReadEventsFromPartitionAsync(
            string partitionId, EventPosition startingPosition, [EnumeratorCancellation] CancellationToken cancellationToken = default)
        {
            // A reconnect after a failed loop reads nothing further, so a failure surfaces as lost events.
            if (_reads.AddOrUpdate(partitionId, 1, (_, reads) => reads + 1) > 1)
                yield break;
            if (Interlocked.Increment(ref _started) == partitions.Count)
                _allStarted.TrySetResult();
            await _allStarted.Task.WaitAsync(TimeSpan.FromSeconds(10), cancellationToken);
            int active = Interlocked.Increment(ref _active);
            for (int seen = Volatile.Read(ref _maxActive); active > seen; seen = Volatile.Read(ref _maxActive))
                Interlocked.CompareExchange(ref _maxActive, active, seen);
            try
            {
                var context = EventHubsModelFactory.PartitionContext("fixture.servicebus.windows.net", "telemetry", "companion", partitionId, default);
                var events = partitions[partitionId];
                for (int i = 0; i < events.Length; i++)
                {
                    if (i % 16 == 0)
                        await Task.Yield();
                    yield return new PartitionEvent(context, new EventData(BinaryData.FromString(events[i])));
                }
            }
            finally
            {
                Interlocked.Decrement(ref _active);
            }
        }
    }

    internal sealed class RecordingLogger : ILogger<EventHubReader>
    {
        private readonly ConcurrentQueue<string> _problems = new();
        public IReadOnlyCollection<string> Problems => _problems;
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
        public bool IsEnabled(LogLevel logLevel) => true;
        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception,
            Func<TState, Exception?, string> formatter)
        {
            if (logLevel >= LogLevel.Warning)
                _problems.Enqueue($"{logLevel}: {formatter(state, exception)} {exception?.GetType().Name} {exception?.Message}");
        }
    }
}
