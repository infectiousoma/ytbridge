
# ytbridge Roadmap & Design Notes (✅ = done / ☑ in progress / ☐ not started)

This document is the single source of truth for features and ideas across **ytbridge**, the **Jellyfin plugin**, and optional **companions** (Telegram bot integration, self-hosted video server).

---

## Current Status
- ✅ FastAPI service (`ytbridge`) with endpoints:
  - ✅ `/search`, `/channel`, `/playlist`, `/item` via Invidious/Piped
  - ✅ `/resolve` via yt-dlp
  - ☑ `/play` proxy with sturdier Range support & 403/410 auto-refresh
- ✅ Docker Compose + `.env` provided
- ☑ **Redis cache** integrated (yt-dlp JSON + merged metadata)

## Next Up (implementation order)
1. ☑ **Redis caching** for yt-dlp dumps + merged metadata
2. ☑ **Sturdier `/play`**: consistent Range behavior, 403/410 auto-refresh, better header passthrough
3. ☐ **Jellyfin plugin scaffold** (settings UI, virtual items, resolver)

---

## Backlog / Options (with checkboxes)

### Streaming & Playback
- ☑ Improve `/play` proxy:
  - ☑ Forward `Range`/`If-Range`, normalize 206 responses when appropriate
  - ☑ Preserve `Content-Type`, `Content-Length`, `Accept-Ranges`, `Content-Range`, `Last-Modified`, `ETag`
  - ☑ Retry by **re-resolving** on upstream 403/410 (expired URL)
  - ☐ Optional bandwidth throttling
- ☐ Per-client **format policy** mapping (e.g., Roku vs web)

### Caching & Persistence
- ☑ **Redis** for:
  - ☑ `yt-dlp --dump-json` by `video_id`
  - ☑ merged metadata cache for `/item`
  - ☐ cache invalidation hooks (manual purge, TTL tuning)
- ☐ Add SQLite/Postgres to persist local entities (favorites, downloads, provenance)

### Favorites & Subscriptions
- ☐ **Favorites system** (FreeTube-like):
  - ☐ Local (Google-free by default)
  - ☐ Import/export as JSON
  - ☐ Optional: subscribe via Invidious/Piped or RSS
- ☐ **Optional Google account** support (off by default):
  - ☐ Use cookies/OAuth to sync watch later/likes/subscriptions
  - ☐ Strict “privacy-first” mode (never contacts Google unless explicitly enabled)

### Downloading & Library
- ☐ **Stream vs Download** modes:
  - ☐ `/download?video_id=...` endpoint (server-side download, tracked in DB)
  - ☐ `/downloads` listing with status/progress
  - ☐ Serve downloaded assets to Jellyfin plugin as a separate library/folder
- ☐ **Shared storage** with Telegram bot companion:
  - ☐ Common volume for downloads
  - ☐ Ingest downloaded files into local index
  - ☐ Store and surface original YouTube URL + full metadata
- ☐ Source selection per item:
  - ☐ Toggle “Pull via YouTube” vs “via Invidious/Piped (self or public instance)”

### Companions
- ☐ **Telegram bot companion** (not part of ytbridge, but complementary):
  - ☐ Reuse **yt-dlp** inside the **ytbridge** container (bind mount binary or call API)
  - ☐ Drop files into shared volume; ytbridge indexes them
- ☐ **Self-hosted YouTube-style media server** companion:
  - ☐ Surface and stream locally downloaded videos (from Jellyfin plugin or Telegram bot)
  - ☐ Acts as an internal catalog with search, tags, channels

### SponsorBlock
- ☐ Chapter mapping in metadata (visible in player timeline)
- ☐ Optional server-side skip inside `/play` (seek over sponsor segments)

### Admin & UX
- ☐ Backend admin routes or small web UI (test search/resolve, view cache, manage favorites/downloads)
- ☐ Jellyfin scheduled sync task with per-source intervals

---

## Data Model (planned)
- **Redis**: cache yt-dlp JSON + merged metadata (TTL: 6–24h).
- **SQLite/Postgres (future)**: favorites, downloads, provenance:
  - Videos: `video_id`, `title`, `duration`, `source_url`, `origin` (yt/invidious/piped), `downloaded_path`, `added_at`
  - Favorites: `kind` (video/channel/playlist/search), `external_id`, `label`, `notes`
  - Downloads: `video_id`, `status`, `progress`, `bytes`, `error`, `created_at`, `updated_at`

---

## Endpoints (planned additions)
- `/favorites` (GET/POST/DELETE) — manage favorites (local JSON persistence initially)
- `/download`, `/downloads` — enqueue and list downloads
- `/ingest` — scan shared volume and index files/metadata
- `/sources` — configure Invidious/Piped/YouTube preferences per request or default

---

## Security & Privacy Defaults
- “Google-free by default”: no Google account usage unless explicitly enabled.
- Cookies/OAuth only if the user turns it on (for age-gated videos/subscriptions sync).
- All secrets (cookies, OAuth tokens) stored only on your server.

---

## Companion Integration Notes
- **Telegram bot**:
  - Share a volume: `/data/downloads` mounted into both containers.
  - Option 1: Bot calls `ytbridge` `/resolve` or future `/download` API.
  - Option 2: Bot uses its own yt-dlp but writes to shared storage; `ytbridge` `/ingest` indexes files + metadata and keeps the **original YouTube URL**.
- **Self-hosted media server**:
  - Independent app that scans the same storage and exposes a YouTube-like UI for your downloaded library.
  - Jellyfin plugin can point to these local files as a separate library for direct play.
