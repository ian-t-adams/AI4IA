using System.Diagnostics;
using System.Runtime.CompilerServices;
using OpenTelemetry;
using OpenTelemetry.Trace;
using SimpleL7Proxy.Events;

namespace AI4IA.Proxy.Tests;

[TestClass]
public sealed class RequestFilterTelemetryProcessorTests
{
    [TestMethod]
    public void AutoCollectedHttpDependenciesAreSuppressed()
    {
        var flags = Process(
            ActivityKind.Client,
            activity => activity.SetTag("http.request.method", "GET"));

        Assert.AreEqual(ActivityTraceFlags.None, flags & ActivityTraceFlags.Recorded);
    }

    [TestMethod]
    public void CustomHttpDependenciesRemainRecorded()
    {
        var flags = Process(
            ActivityKind.Client,
            activity =>
            {
                activity.SetTag("http.request.method", "POST");
                activity.SetTag("Ver", "test");
            });

        Assert.AreEqual(ActivityTraceFlags.Recorded, flags & ActivityTraceFlags.Recorded);
    }

    [TestMethod]
    public void UnmarkedRequestsAreSuppressed()
    {
        var flags = Process(ActivityKind.Server);

        Assert.AreEqual(ActivityTraceFlags.None, flags & ActivityTraceFlags.Recorded);
    }

    [TestMethod]
    public void NonHttpDependenciesRemainRecorded()
    {
        var flags = Process(
            ActivityKind.Client,
            activity => activity.SetTag("db.system.name", "cosmosdb"));

        Assert.AreEqual(ActivityTraceFlags.Recorded, flags & ActivityTraceFlags.Recorded);
    }

    [TestMethod]
    public void FilterBeforeExporterSuppressesAutoHttpDependencies()
    {
        var exported = Export(
            activity => activity.SetTag("http.request.method", "GET"));

        Assert.AreEqual(0L, exported);
    }

    [TestMethod]
    public void CustomHttpDependenciesReachExporter()
    {
        var exported = Export(
            activity =>
            {
                activity.SetTag("http.request.method", "POST");
                activity.SetTag("CustomTracked", "true");
            });

        Assert.AreEqual(1L, exported);
    }

    [TestMethod]
    public void ProgramRegistersFilterBeforeApplicationInsightsExporter()
    {
        var testDirectory = Path.GetDirectoryName(SourceFile())!;
        var program = File.ReadAllText(
            Path.GetFullPath(
                Path.Combine(testDirectory, "..", "SimpleL7Proxy", "Program.cs")));
        var filterIndex = program.IndexOf(
            "ConfigureOpenTelemetryTracerProvider",
            StringComparison.Ordinal);
        var exporterIndex = program.IndexOf(
            "AddApplicationInsightsTelemetryWorkerService",
            StringComparison.Ordinal);

        Assert.IsTrue(
            filterIndex >= 0 && exporterIndex > filterIndex,
            "The AI4IA filter must be registered before Application Insights adds its exporter.");
    }

    private static ActivityTraceFlags Process(
        ActivityKind kind,
        Action<Activity>? configure = null)
    {
        var sourceName = $"AI4IA.Proxy.Tests.{Guid.NewGuid():N}";
        using var listener = new ActivityListener
        {
            ShouldListenTo = source => source.Name == sourceName,
            Sample = static (ref ActivityCreationOptions<ActivityContext> _) =>
                ActivitySamplingResult.AllDataAndRecorded,
            SampleUsingParentId = static (ref ActivityCreationOptions<string> _) =>
                ActivitySamplingResult.AllDataAndRecorded,
        };
        ActivitySource.AddActivityListener(listener);

        using var source = new ActivitySource(sourceName);
        using var activity = source.StartActivity("test", kind);
        Assert.IsNotNull(activity);
        configure?.Invoke(activity);

        new RequestFilterTelemetryProcessor().OnEnd(activity);
        return activity.ActivityTraceFlags;
    }

    private static long Export(Action<Activity> configure)
    {
        var sourceName = $"AI4IA.Proxy.Tests.Export.{Guid.NewGuid():N}";
        var exporter = new RecordingExporter();
        using var provider = Sdk.CreateTracerProviderBuilder()
            .AddSource(sourceName)
            .AddProcessor(new RequestFilterTelemetryProcessor())
            .AddProcessor(new SimpleActivityExportProcessor(exporter))
            .Build();
        using var source = new ActivitySource(sourceName);
        using (var activity = source.StartActivity("test", ActivityKind.Client))
        {
            Assert.IsNotNull(activity);
            configure(activity);
        }
        provider.ForceFlush();
        return exporter.ExportedActivities;
    }

    private static string SourceFile([CallerFilePath] string path = "") => path;

    private sealed class RecordingExporter : BaseExporter<Activity>
    {
        public long ExportedActivities { get; private set; }

        public override ExportResult Export(in Batch<Activity> batch)
        {
            ExportedActivities += batch.Count;
            return ExportResult.Success;
        }
    }
}
