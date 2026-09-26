using CompanionApp.Components;
using CompanionApp.Components.Shared;
using CompanionApp.Components.Shared.EventHub;
using CompanionApp.Ai4ia;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.Extensions.Options;

var builder = WebApplication.CreateBuilder(args);
// AI4IA: the chat/vision presets and pages are not vendored; Event Hubs uses managed identity only.
HostedGuard.RequireManagedIdentityOnly(builder.Configuration);
var adminAccess = HostedGuard.RequireAdminAllowlist(builder.Configuration);

var eventHubSection = builder.Configuration.GetSection(EventHubMonitorOptions.SectionName);
var eventHubEnabled = eventHubSection.GetValue<bool>("eventhub_enabled", true);
var localEventFilePath = eventHubSection.GetValue<string>("LocalFilePath");

// Add services to the container.
builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();
builder.Services.AddDataProtection()
    .SetApplicationName("chat_tester")
    // AI4IA: keys live outside the content root when configured, never in the source tree.
    .PersistKeysToFileSystem(new DirectoryInfo(builder.Configuration["CompanionApp:DataProtectionKeysPath"]
        ?? Path.Combine(builder.Environment.ContentRootPath, ".keys")));
// AI4IA: no page may send server-side requests to caller-chosen URLs or forward headers.
builder.Services.AddSingleton(HostedGuard.CreateRefusingHttpClient());
builder.Services.AddSingleton<AuthTokenSettings>();
builder.Services.AddSingleton<UserSettings>();
builder.Services.AddSingleton<HeaderSettings>();
builder.Services.AddSingleton<HistorySettings>();
builder.Services.AddSingleton<ConversationSettings>();
builder.Services.AddSingleton<RequestDebugSettings>();
builder.Services.AddSingleton<AutoCollapseSettings>();
builder.Services.AddSingleton<ModelDefaults>();
builder.Services.AddSingleton<VisionModelCatalog>();
builder.Services.AddSingleton<EventHubMonitorStore>();
builder.Services.AddSingleton<ProxyMetricsCatalog>();
if (eventHubEnabled || !string.IsNullOrWhiteSpace(localEventFilePath))
{
    builder.Services.AddHostedService<EventHubReader>();
}
builder.Services.AddScoped<UserPreferencesService>();
builder.Services.Configure<CompanionAppOptions>(
    builder.Configuration.GetSection(CompanionAppOptions.SectionName));
builder.Services.Configure<CompanionAppOptions>(options =>
{
    options.AppConfigurationRules = builder.Configuration
        .GetSection($"{CompanionAppOptions.UiSectionName}:AppConfigurationRules")
        .Get<List<AppConfigurationSettingRule>>() ?? new();
    options.Hosts = builder.Configuration
        .GetSection($"{CompanionAppOptions.UiSectionName}:Hosts")
        .Get<AppConfigHostSettings>() ?? new();
});
builder.Services.Configure<EventHubMonitorOptions>(
    builder.Configuration.GetSection(EventHubMonitorOptions.SectionName));

var app = builder.Build();
// AI4IA: first in the pipeline, so nothing else runs for a non-admin request.
app.Use(HostedGuard.AdminOnly(adminAccess));
var companionAppOptions = app.Services.GetRequiredService<IOptions<CompanionAppOptions>>().Value;
app.Services.GetRequiredService<HistorySettings>()
    .ApplyDefaultsIfMissing(companionAppOptions.History);
app.Services.GetRequiredService<ConversationSettings>()
    .ApplyDefaultsIfMissing(companionAppOptions.Conversations);

// Configure the HTTP request pipeline.
if (!app.Environment.IsDevelopment())
{
    app.UseExceptionHandler("/Error", createScopeForErrors: true);
    // The default HSTS value is 30 days. You may want to change this for production scenarios, see https://aka.ms/aspnetcore-hsts.
    app.UseHsts();
}
app.UseStatusCodePagesWithReExecute("/not-found", createScopeForStatusCodePages: true);
app.UseHttpsRedirection();

app.UseAntiforgery();

app.MapStaticAssets();
app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
