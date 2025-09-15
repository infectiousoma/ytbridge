# yt_bridge.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
import os, subprocess, json, urllib.parse, httpx, redis, shlex

# ---------- Config ----------
BACKEND_PROVIDER = os.environ.get("BACKEND_PROVIDER", "invidious").strip().lower()
BACKEND_BASE     = os.environ.get("BACKEND_BASE", "https://yewtu.be").rstrip("/")
COOKIES          = os.environ.get("YTDLP_COOKIES", "").strip()
SPONSORBLOCK     = os.environ.get("SPONSORBLOCK", "true").strip().lower()
PORT             = int(os.environ.get("PORT", "8080"))

# yt-dlp source selection (no bundling required)
YTDLP_MODE        = os.environ.get("YTDLP_MODE", "local").strip().lower()    # "local" | "remote"
YTDLP_CMD         = os.environ.get("YTDLP_CMD", "yt-dlp").strip()            # path to external binary
YTDLP_REMOTE_URL  = os.environ.get("YTDLP_REMOTE_URL", "").strip()           # e.g. http://ytdlp-api:3030/dump

# Cache (Redis)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_TTL = int(os.environ.get("REDIS_TTL", "43200"))  # 12h

app = FastAPI(title="ytbridge", version="0.3.0")
rds = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ---------- Cache helpers ----------
def cache_get(key: str):
    try:
        return rds.get(key)
    except Exception:
        return None

def cache_set(key: str, value: str, ttl: int = REDIS_TTL):
    try:
        rds.setex(key, ttl, value)
    except Exception:
        pass

# ---------- yt-dlp adapters ----------

def _build_local_cmd(url: str) -> list[str]:
    # Compose: <YTDLP_CMD> <url> --dump-json --no-warnings [--cookies file] [--sponsorblock-mark all]
    cmd = [YTDLP_CMD, url, "--dump-json", "--no-warnings"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    if SPONSORBLOCK == "true":
        cmd += ["--sponsorblock-mark", "all"]
    return cmd

async def _remote_ytdlp_dump(url: str) -> dict:
    """
    Expect a remote endpoint that returns the raw JSON of `yt-dlp -J <url>`.
    Default contract: GET {YTDLP_REMOTE_URL}?url=<url>
    """
    if not YTDLP_REMOTE_URL:
        raise HTTPException(500, "YTDLP_REMOTE_URL not set for remote mode")
    q = {"url": url}
    if COOKIES:
        # Optional: remote service may use this
        q["cookies"] = COOKIES
    if SPONSORBLOCK == "true":
        q["sponsorblock"] = "all"

    async with httpx.AsyncClient(timeout=60) as cx:
        try:
            r = await cx.get(YTDLP_REMOTE_URL, params=q)
        except Exception as e:
            raise HTTPException(502, f"yt-dlp remote error: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"yt-dlp remote status {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(502, "yt-dlp remote returned non-JSON")

def _local_ytdlp_dump(url: str) -> dict:
    cmd = _build_local_cmd(url)
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise HTTPException(500, f"yt-dlp not found at '{YTDLP_CMD}'. Set YTDLP_CMD or mount the binary.")
    except subprocess.CalledProcessError as e:
        tail = (e.output or "")[-400:]
        raise HTTPException(502, f"yt-dlp failed: {tail}")
    try:
        return json.loads(out)
    except Exception:
        raise HTTPException(502, "Failed to parse yt-dlp JSON")

def ytdlp_dump(video_id: str) -> dict:
    """
    Cached JSON probe equivalent to `yt-dlp -J`.
    Uses Redis and supports either local binary or remote service.
    """
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"

    if YTDLP_MODE == "remote":
        info = httpx.run(_remote_ytdlp_dump(url))  # type: ignore[attr-defined]
        # ^ httpx.run is not real; see below: we'll wrap with anyio
    else:
        info = _local_ytdlp_dump(url)

    # Persist cache
    try:
        cache_set(ck, json.dumps(info))
    except Exception:
        pass
    return info

# Small shim because we can't await inside sync ytdlp_dump()
import anyio
def _await(coro):
    return anyio.run(lambda: coro)

def ytdlp_dump(video_id: str) -> dict:  # re-define using the shim
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"
    if YTDLP_MODE == "remote":
        info = _await(_remote_ytdlp_dump(url))
    else:
        info = _local_ytdlp_dump(url)

    try:
        cache_set(ck, json.dumps(info))
    except Exception:
        pass
    return info

def pick_stream(info: dict, policy: str = "h264_mp4") -> dict:
    formats = info.get("formats") or []
    best = None
    if policy == "h264_mp4":
        mp4s = [f for f in formats
                if (f.get("container") == "mp4" or f.get("ext") == "mp4")
                and f.get("acodec") not in (None, "none")
                and f.get("vcodec") not in (None, "none")
                and f.get("url")]
        if mp4s:
            best = max(mp4s, key=lambda f: f.get("tbr") or 0)
    if not best:
        muxed = [f for f in formats if f.get("acodec") not in (None, "none")
                 and f.get("vcodec") not in (None, "none") and f.get("url")]
        if muxed:
            best = max(muxed, key=lambda f: f.get("tbr") or 0)
    if not best and info.get("url"):
        best = {"url": info["url"], "ext": info.get("ext"), "container": info.get("container"),
                "vcodec": info.get("vcodec"), "acodec": info.get("acodec")}
    if not best or not best.get("url"):
        raise HTTPException(status_code=502, detail="No playable stream found")
    container = best.get("container") or best.get("ext") or "mp4"
    v = best.get("vcodec") or ""
    a = best.get("acodec") or ""
    return {"url": best["url"], "container": container, "codecs": f"{v}+{a}".strip("+")}

async def backend_get(path: str, params: dict | None = None) -> httpx.Response:
    url = f"{BACKEND_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.get(url, params=params)
        return r

# ---------- Routes ----------
@app.get("/healthz")
async def healthz():
    # Surface which mode we’re using for debugging
    return JSONResponse({"ok": True, "ytdlp_mode": YTDLP_MODE, "ytdlp_cmd": YTDLP_CMD, "remote": YTDLP_REMOTE_URL or None})

@app.get("/search")
async def search(q: str, type: str = "video", page: int = 1, limit: int = 30):
    type_map = {"video": "video", "channel": "channel", "playlist": "playlist"}
    if type not in type_map:
        raise HTTPException(400, "Invalid type")
    if BACKEND_PROVIDER == "invidious":
        r = await backend_get("/api/v1/search", {"q": q, "page": page, "type": type_map[type]})
    elif BACKEND_PROVIDER == "piped":
        r = await backend_get("/api/v1/search", {"q": q})
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream search error: {r.text[:200]}")
    data = r.json()
    if isinstance(data, list):
        data = data[:limit]
    return JSONResponse(data)

@app.get("/channel/{channel_id}")
async def channel(channel_id: str, page: int = 1):
    if BACKEND_PROVIDER == "invidious":
        r = await backend_get(f"/api/v1/channels/{channel_id}/videos", {"page": page})
    elif BACKEND_PROVIDER == "piped":
        r = await backend_get(f"/api/v1/channel/{channel_id}")
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream channel error: {r.text[:200]}")
    return JSONResponse(r.json())

@app.get("/playlist/{playlist_id}")
async def playlist(playlist_id: str, page: int = 1):
    if BACKEND_PROVIDER == "invidious":
        r = await backend_get(f"/api/v1/playlists/{playlist_id}", {"page": page})
    elif BACKEND_PROVIDER == "piped":
        r = await backend_get(f"/api/v1/playlist/{playlist_id}")
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream playlist error: {r.text[:200]}")
    return JSONResponse(r.json())

@app.get("/item/{video_id}")
async def item(video_id: str):
    ckey = f"meta:item:{video_id}"
    cached = cache_get(ckey)
    if cached:
        try:
            return JSONResponse(json.loads(cached))
        except Exception:
            pass

    if BACKEND_PROVIDER == "invidious":
        r = await backend_get(f"/api/v1/videos/{video_id}")
    elif BACKEND_PROVIDER == "piped":
        r = await backend_get(f"/api/v1/video/{video_id}")
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream item error: {r.text[:200]}")
    meta = r.json()

    # Enrich with yt-dlp data (chapters, subs, thumbs, duration)
    try:
        info = ytdlp_dump(video_id)
        meta["chapters"]  = info.get("chapters") or []
        meta["subtitles"] = info.get("subtitles") or {}
        meta["duration"]  = info.get("duration") or meta.get("lengthSeconds")
        thumbs = info.get("thumbnails") or []
        if thumbs:
            meta["thumbnails"] = thumbs
    except HTTPException:
        pass

    try:
        cache_set(ckey, json.dumps(meta))
    except Exception:
        pass
    return JSONResponse(meta)

@app.get("/resolve")
async def resolve(video_id: str, policy: str = "h264_mp4"):
    info = ytdlp_dump(video_id)
    stream = pick_stream(info, policy)
    payload = {
        "id": video_id,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnails": info.get("thumbnails"),
        "chapters": info.get("chapters") or [],
        "subtitles": info.get("subtitles") or {},
        **stream
    }
    return JSONResponse(payload)

async def probe_headers(target_url, headers):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cx:
        hr = await cx.head(target_url, headers=headers)
    return hr

@app.get("/play/{video_id}")
async def play(video_id: str, request: Request, policy: str = "h264_mp4"):
    info = ytdlp_dump(video_id)
    stream = pick_stream(info, policy)
    target = stream["url"]

    headers = {}
    range_hdr = request.headers.get("Range")
    if range_hdr:
        headers["Range"] = range_hdr
    if request.headers.get("If-Range"):
        headers["If-Range"] = request.headers.get("If-Range")

    async def generator(target_url):
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as cx:
            async with cx.stream("GET", target_url, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    try:
        hr = await probe_headers(target, headers)
    except Exception:
        hr = None

    if hr is not None and hr.status_code in (403, 410):
        info2 = ytdlp_dump(video_id)
        stream2 = pick_stream(info2, policy)
        target = stream2["url"]
        try:
            hr = await probe_headers(target, headers)
        except Exception:
            hr = None

    resp_headers = {}
    status = 200
    if hr is not None:
        status = 206 if range_hdr and hr.status_code in (200, 206) else 200
        for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Last-Modified", "ETag"]:
            if h in hr.headers:
                resp_headers[h] = hr.headers[h]

    return StreamingResponse(generator(target), status_code=status, headers=resp_headers)
