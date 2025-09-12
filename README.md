# ytbridge — Jellyfin backend with yt-dlp + optional Invidious/Piped

A tiny HTTP service that lets a Jellyfin plugin (or any client) **search/browse** YouTube via **Invidious or Piped** and **resolve** clean, ad-free streams via **yt-dlp**. You get fast search/metadata plus resilient playback—without embedding a browser or adblocker.

---

## Features

- **Search/Browse** via Invidious or Piped (no Google API keys).
- **Resolve Playback** via `yt-dlp` (`/resolve`), with optional `/play` proxy that supports **HTTP Range** for reliable seeking/transcoding.
- **SponsorBlock** chapter marks (optional).
- Pass-through **subtitles** and **chapters** from yt-dlp.
- Simple, environment-only configuration.

---

## Architecture

```
Jellyfin Plugin  ──(HTTP)──>  ytbridge (this service)
   Search/List                  ├─ talks to Invidious/Piped for metadata
   Playback                     └─ runs yt-dlp to get clean media URLs (or proxies)
```

Use **Invidious/Piped** for *find/describe*; use **yt-dlp** for *play/resolve*.

---

## Endpoints

- `GET /search?q=QUERY&limit=50&type=video|channel|playlist&page=1`
  - Uses Invidious or Piped. Returns normalized JSON from the upstream API.
- `GET /channel/{channel_id}?page=1`
  - Lists channel videos.
- `GET /playlist/{playlist_id}?page=1`
  - Lists playlist items.
- `GET /item/{video_id}`
  - Video metadata from Invidious/Piped. (Optionally enriched with yt-dlp chapters/subtitles if available.)
- `GET /resolve?video_id={ID}&policy=h264_mp4`
  - Runs yt-dlp to select a playable stream per policy; returns JSON including `url`, `container`, `codecs`, `duration`, `thumbnails`, `chapters`, `subtitles`.
- `GET /play/{video_id}?policy=h264_mp4`
  - **Proxy**: Streams bytes from resolved URL with **Accept-Ranges** support. Recommended for Jellyfin clients.
- `GET /healthz`
  - Liveness probe.

---

## Policies

The `policy` query controls how we pick formats. Included policy:
- `h264_mp4` (default): prefers progressive MP4 H.264 + AAC for broad client support (TVs, Rokus, older browsers).

You can extend `pick_stream()` to add more (e.g., VP9/Opus, AV1).

---

## Environment Variables

| Var | Default | Description |
|---|---|---|
| `BACKEND_PROVIDER` | `invidious` | One of `invidious` or `piped`. |
| `BACKEND_BASE` | `https://yewtu.be` | Base URL of your Invidious/Piped instance. |
| `YTDLP_COOKIES` | *(empty)* | Path to a cookies.txt file for age-gated content. Optional. |
| `SPONSORBLOCK` | `true` | `"true"` to add SponsorBlock chapter marks (`--sponsorblock-mark all`). |
| `PORT` | `8080` | HTTP port for this service. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis URL for caching (optional but recommended). |
| `REDIS_TTL` | `43200` | Cache TTL in seconds (default 12h). |

**Tip:** Self-host Invidious or Piped for reliability, or point to a public instance you trust.

---

## Quick Start

### 1) Download the files

- `README.md`
- `app.py`
- `docker-compose.yml`
- `.env.example`

### 2) (Optional) Self-host Invidious (or Piped)
If you prefer local metadata/search, deploy your own Invidious/Piped and put its URL into `.env` (see below).

### 3) Configure `.env`

Copy the example and edit if needed:

```bash
cp .env.example .env
# then edit .env to set BACKEND_BASE to your Invidious/Piped URL
```

### 4) Launch

```bash
docker compose up -d --build
```

The service will listen on the port you set (default **8080**).

---

## Example Requests

```bash
# Search videos
curl 'http://localhost:8080/search?q=lofi&type=video&limit=10'

# Resolve a video to a direct playable URL
curl 'http://localhost:8080/resolve?video_id=dQw4w9WgXcQ' | jq .

# Proxy play (Jellyfin should use this URL as the media source)
curl -I 'http://localhost:8080/play/dQw4w9WgXcQ'

# Channel listing
curl 'http://localhost:8080/channel/UC-9-kyTW8ZkZNDHQJ6FgpwQ?page=1'

# Health
curl 'http://localhost:8080/healthz'
```

---

## Wiring to Jellyfin (plugin outline)

In your Jellyfin plugin (.NET):

- **Settings:** backend base URL (this service), preferred policy, followed channels/playlists, sync interval.
- **Virtual Items:** call `/channel/:id`, `/playlist/:id`, or `/search` to build library items. Store `ProviderIds["YouTube"]=video_id` and thumbnail URL.
- **Metadata Provider:** call `/item/:id` for overview, date, duration, thumbs.
- **Stream Resolver:** call `/resolve` *or* directly set `Path="http://ytbridge:8080/play/{id}"` with `Protocol=Http`, `Container="mp4"`, `SupportsDirectPlay=true`.
- **Chapters/Subtitles:** map from `/resolve` payload to `ChapterInfo` and `MediaStream` entries.

This keeps Jellyfin “pure”: it never runs yt-dlp, never touches cookies, and doesn’t depend on public instances.

---

## Notes & Tips

- **Caching:** For production, add a small cache around yt-dlp (e.g., Redis + TTL by `video_id`). The current app is stateless for clarity.
- **Rate limiting:** Consider a simple token bucket (per-IP or global) to avoid upstream throttling.
- **Geo/age restrictions:** Provide a cookies file and, if needed, proxy settings to yt-dlp.
- **ToS:** Using YouTube outside their official API may violate their ToS. Use for personal/educational purposes at your discretion.

---

## License

MIT — do what you want, no liability implied.