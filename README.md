# ytbridge — Jellyfin backend with yt-dlp + optional Invidious/Piped

A tiny HTTP service that lets a Jellyfin plugin (or any client) **search/browse** YouTube via **Invidious or Piped** and **resolve** clean, ad-free streams via **yt-dlp**. You get fast search/metadata plus resilient playback—without embedding a browser or adblocker.

---

## Features

- **Search/Browse** via Invidious or Piped (no Google API keys).
- **Resolve Playback** via `yt-dlp` (`/resolve`), with optional `/play` proxy that supports **HTTP Range** for reliable seeking/transcoding.
- **SponsorBlock** chapter marks (optional).
- Pass-through **subtitles** and **chapters** from yt-dlp.
- **Favorites** and **Subscriptions** import/export (JSON, OPML, FreeTube formats).
- Simple, environment-only configuration.

---

## Architecture

```
Jellyfin Plugin  ──(HTTP)──>  ytbridge (this service)
   Search/List                  ├─ talks to Invidious/Piped for metadata
   Playback                     └─ runs yt-dlp to get clean media URLs (or proxies)
   Favorites/Subs                └─ JSON/OPML files stored in priv/data/
```

---

## Endpoints

- **Core**
  - `GET /search?q=QUERY&limit=50&type=video|channel|playlist&page=1`
  - `GET /channel/{channel_id}?page=1`
  - `GET /item/{video_id}`
  - `GET /formats/{video_id}`
  - `GET /resolve?video_id={ID}&policy=h264_mp4`
  - `GET /play/{video_id}?policy=h264_mp4`
  - `HEAD /play/{video_id}`
  - `GET /healthz`
- **Subscriptions**
  - `GET /subscriptions`
  - `POST /subscriptions/import` (accepts JSON/OPML)
  - `GET /subscriptions/export?format=opml|json|freetube`
- **Favorites**
  - `GET /favorites`
  - `POST /favorites/import`
  - `GET /favorites/export`
  - `POST /favorites/add`

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
| `YTDLP_MODE` | `local` | `"local"` or `"remote"` yt-dlp mode. |
| `YTDLP_CMD` | `yt-dlp` | Path to yt-dlp binary (if local mode). |
| `YTDLP_REMOTE_URL` | *(empty)* | URL of remote yt-dlp service (if remote mode). |
| `YTDLP_COOKIES` | *(empty)* | Path to a cookies.txt file for age-gated content. |
| `SPONSORBLOCK` | `true` | `"true"` to add SponsorBlock marks. |
| `PORT` | `8080` | HTTP port for this service. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis URL for caching. |
| `REDIS_TTL` | `43200` | Cache TTL in seconds (12h). |
| `DATA_DIR` | `/app/priv/data` | Directory for subscriptions/favorites JSON. |
| `FFMPEG_CMD` | `ffmpeg` | Path to ffmpeg binary for live remux. |

---

## Project Layout

```
ytbridge-refactor/
├── Dockerfile
├── docker-compose.yaml
├── README.md
├── src/
│   ├── ytbridge.py        # main FastAPI app
│   ├── cache.py
│   ├── config.py
│   ├── favorites.py
│   ├── subscriptions.py
│   ├── ytdlp.py
│   ├── formats.py
│   ├── routes.py
│   └── utils.py
└── priv/
    ├── cookies.txt        # optional
    └── data/
        ├── favorites.json
        └── subscriptions.json
```

---

## Quick Start

### 1) Clone & build

```bash
git clone https://github.com/yourname/ytbridge
cd ytbridge-refactor
docker compose up -d --build
```

### 2) (Optional) Self-host Invidious/Piped

Update `BACKEND_BASE` in your `.env` or `docker-compose.yaml`.

### 3) Mount cookies / persist data

Bind-mount `./priv:/app/priv` to persist JSON files and provide cookies.

### 4) Check health

```bash
curl http://localhost:8080/healthz | jq .
```

---

## Example Requests

```bash
# Search videos
curl 'http://localhost:8080/search?q=lofi&type=video&limit=10'

# Resolve video
curl 'http://localhost:8080/resolve?video_id=dQw4w9WgXcQ' | jq .

# Stream play
curl -I 'http://localhost:8080/play/dQw4w9WgXcQ'

# Export subscriptions
curl 'http://localhost:8080/subscriptions/export?format=opml'

# Add favorite
curl -X POST -F "video_id=dQw4w9WgXcQ" -F "title=Never Gonna Give You Up" http://localhost:8080/favorites/add
```

---

## Jellyfin Integration

In your Jellyfin plugin:

- **Settings:** backend base URL (this service), preferred policy, channels/playlists, sync interval.
- **Virtual Items:** call `/channel/:id`, `/playlist/:id`, or `/search` to build library items.
- **Metadata Provider:** call `/item/:id` for overview, date, duration, thumbs.
- **Stream Resolver:** call `/resolve` *or* set `Path="http://ytbridge:8080/play/{id}"`.
- **Chapters/Subtitles:** map from `/resolve` payload.

---

## Notes & Tips

- **Caching:** Redis recommended to avoid repeating yt-dlp calls.
- **Cookies:** Put `cookies.txt` in `priv/` and set `YTDLP_COOKIES=/app/priv/cookies.txt`.
- **Persistence:** Subscriptions/favorites stored in `priv/data/`.
- **ToS:** This may violate YouTube ToS; use responsibly.

---

## License

MIT — do what you want, no liability implied.
