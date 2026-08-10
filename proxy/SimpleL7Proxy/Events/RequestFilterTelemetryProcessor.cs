using System.Diagnostics;
using OpenTelemetry;

namespace SimpleL7Proxy.Events;

/// <summary>
/// Filters auto-collected HTTP dependencies and unmarked requests while preserving
/// telemetry emitted explicitly by <see cref="ProxyEvent"/>.
/// </summary>
public sealed class RequestFilterTelemetryProcessor : BaseProcessor<Activity>
{
    public override void OnEnd(Activity activity)
    {
        if (HasAi4iaMarker(activity))
        {
            return;
        }

        var isHttpDependency =
            activity.Kind == ActivityKind.Client
            && (
                activity.Source.Name.StartsWith("System.Net.Http", StringComparison.Ordinal)
                || activity.GetTagItem("http.request.method") is not null
                || activity.GetTagItem("http.method") is not null
                || string.Equals(
                    activity.GetTagItem("microsoft.dependency.type")?.ToString(),
                    "http",
                    StringComparison.OrdinalIgnoreCase)
            );
        var isRequest =
            activity.Kind is ActivityKind.Server or ActivityKind.Consumer;

        if (isHttpDependency || isRequest)
        {
            activity.ActivityTraceFlags &= ~ActivityTraceFlags.Recorded;
        }
    }

    private static bool HasAi4iaMarker(Activity activity)
    {
        return activity.GetTagItem("Ver") is not null
            || activity.GetTagItem("Revision") is not null
            || activity.GetTagItem("CustomTracked") is not null;
    }
}
