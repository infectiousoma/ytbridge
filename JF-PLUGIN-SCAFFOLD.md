
# Jellyfin Plugin — YtBridge Scaffold

This is an outline for a Jellyfin plugin that talks to `ytbridge`.

## Project files
```
Jellyfin.Plugin.YtBridge/
  Jellyfin.Plugin.YtBridge.csproj
  plugin.json
  Configuration/Plugin.cs
  Configuration/PluginConfiguration.cs
  Api/YtBridgeClient.cs
  Channels/YtBridgeChannel.cs
  Providers/YtBridgeMetadataProvider.cs
  MediaSources/YtBridgeMediaSourceProvider.cs
```

### `plugin.json` (example)
```json
{
  "name": "YtBridge",
  "version": "0.1.0",
  "description": "Browse and play YouTube via ytbridge (yt-dlp + Invidious/Piped).",
  "id": "com.example.ytbridge",
  "targetAbi": "10.8.0",
  "guid": "e2a6b8b6-16b9-4a65-9b2f-1a2f2d3e5abc"
}
```

### `PluginConfiguration.cs` (example)
```csharp
using MediaBrowser.Model.Plugins;
using System.Collections.Generic;

public class PluginConfiguration : BasePluginConfiguration
{
    public string BackendBaseUrl { get; set; } = "http://ytbridge:8080";
    public string FormatPolicy { get; set; } = "h264_mp4";
    public List<string> Channels { get; set; } = new();
    public List<string> Playlists { get; set; } = new();
    public List<string> Searches { get; set; } = new();
    public int SyncIntervalMinutes { get; set; } = 60;
    public bool UseProxyPlayback { get; set; } = true;
    public bool IncludeSponsorBlockChapters { get; set; } = true;
}
```

### `Plugin.cs` (example)
```csharp
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using System;

public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public override string Name => "YtBridge";
    public override Guid Id => Guid.Parse("e2a6b8b6-16b9-4a65-9b2f-1a2f2d3e5abc");
    public Plugin(IApplicationPaths paths, IXmlSerializer serializer) : base(paths, serializer) { }

    public IEnumerable<PluginPageInfo> GetPages() => new[] {
        new PluginPageInfo { Name = "ytbridge", EmbeddedResourcePath = GetType().Namespace + ".Web.ytbridge.html" }
    };
}
```

### `YtBridgeClient.cs` (example sketch)
```csharp
using System.Net.Http;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

public class YtBridgeClient
{
    private readonly HttpClient _http;
    public string Base { get; }

    public YtBridgeClient(HttpClient http, string backendBaseUrl)
    {
        _http = http;
        Base = backendBaseUrl.TrimEnd('/');
    }

    public async Task<JsonDocument> ResolveAsync(string id, string policy, CancellationToken ct)
    {
        var url = $"{Base}/resolve?video_id={id}&policy={policy}";
        using var resp = await _http.GetAsync(url, ct);
        resp.EnsureSuccessStatusCode();
        var s = await resp.Content.ReadAsStringAsync(ct);
        return JsonDocument.Parse(s);
    }
    // Add Search/Channel/Playlist/Item wrappers similarly
}
```

### `YtBridgeMediaSourceProvider.cs` (example sketch)
```csharp
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

public class YtBridgeMediaSourceProvider : IMediaSourceProvider
{
    private readonly YtBridgeClient _client;
    private readonly PluginConfiguration _cfg;

    public YtBridgeMediaSourceProvider(YtBridgeClient client, PluginConfiguration cfg)
    {
        _client = client;
        _cfg = cfg;
    }

    public async Task<IEnumerable<MediaSourceInfo>> GetMediaSources(BaseItem item, CancellationToken ct)
    {
        var id = item.GetProviderId("YouTube");
        var resolved = await _client.ResolveAsync(id, _cfg.FormatPolicy, ct);
        var root = resolved.RootElement;

        var path = _cfg.UseProxyPlayback
            ? $"{_client.Base}/play/{id}?policy={_cfg.FormatPolicy}"
            : root.GetProperty("url").GetString();

        return new[] {
            new MediaSourceInfo {
                Path = path,
                Protocol = MediaProtocol.Http,
                Container = root.TryGetProperty("container", out var c) ? c.GetString() : "mp4",
                SupportsDirectPlay = true,
                RequiresOpening = false
            }
        };
    }
}
```

### `YtBridgeChannel.cs` (example sketch)
- Implements a virtual “channel”/folder that lists items from configured sources by calling `ytbridge` `/channel`, `/playlist`, `/search`.
- Creates `BaseItem` entries with `Name`, `Overview`, `ImageUrl`, and `ProviderIds["YouTube"]=video_id`.
- Use a scheduled task to periodically refresh items.

## Build & Deploy
- Build with the Jellyfin plugin SDK version matching your server’s `targetAbi`.
- Drop the compiled DLL + `plugin.json` into Jellyfin’s plugins directory and restart.
