# yt_bridge.py
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import os, subprocess, json, httpx, redis, re, pathlib, time
from typing import List, Dict, Any

# ---------- Config ----------
BACKEND_PROVIDER = os.environ.get("BACKEND_PROVIDER", "invidious").strip().lower()
BACKEND_BASE     = os.environ.get("BACKEND_BASE", "https://yewtu.be").rstrip("/")
COOKIES          = os.environ.get("YTDLP_COOKIES", "").strip()
SPONSORBLOCK     = os.environ.get("SPONSORBLOCK", "true").strip().lower()
PORT             = int(os.environ.get("PORT", "8080"))

# yt-dlp source selection
YTDLP_MODE        = os.environ.get("YTDLP_MODE", "local").strip().lower()    # "local" | "remote"
YTDLP_CMD         = os.environ.get("YTDLP_CMD", "yt-dlp").strip()
YTDLP_REMOTE_URL  = os.environ.get("YTDLP_REMOTE_URL", "").strip()

# ffmpeg (for split streams remux)
FFMPEG_CMD        = os.environ.get("FFMPEG_CMD", "ffmpeg").strip()

# Cache (Redis)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_TTL = int(os.environ.get("REDIS_TTL", "43200"))  # 12h

# Persistent data (favorites/subscriptions)
DATA_DIR  = os.environ.get("DATA_DIR", "/data")
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
SUBS_PATH = os.path.join(DATA_DIR, "subscriptions.json")
FAVS_PATH = os.path.join(DATA_DIR, "favorites.json")

app = FastAPI(title="ytbridge", version="0.7.1")
rds = redis.Redis.from_url(REDIS_URL, decode_responses=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# ---------- Local filesystem helpers (subs/favs) ----------
def _load_list(path: str) -> list:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_list(path: str, data: list):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_subscriptions() -> List[Dict[str, Any]]:
    data = _load_list(SUBS_PATH)
    return data if isinstance(data, list) else []

def save_subscriptions(items: List[Dict[str, Any]]):
    seen, out = set(), []
    for it in items:
        cid = it.get("channelId") or it.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"channelId": cid, "title": it.get("title"), "url": it.get("url")})
    _save_list(SUBS_PATH, out)

def load_favorites() -> List[Dict[str, Any]]:
    data = _load_list(FAVS_PATH)
    return data if isinstance(data, list) else []

def save_favorites(items: List[Dict[str, Any]]):
    seen, out = set(), []
    for it in items:
        vid = it.get("videoId") or it.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({"videoId": vid, "title": it.get("title")})
    _save_list(FAVS_PATH, out)

# ---------- yt-dlp adapters ----------
def _build_local_cmd(url: str) -> list[str]:
    cmd = [YTDLP_CMD, url, "--dump-json", "--no-warnings"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    if SPONSORBLOCK == "true":
        cmd += ["--sponsorblock-mark", "all"]
    return cmd

def _remote_ytdlp_dump(url: str) -> dict:
    if not YTDLP_REMOTE_URL:
        raise HTTPException(500, "YTDLP_REMOTE_URL not set for remote mode")
    q = {"url": url}
    if COOKIES:
        q["cookies"] = COOKIES
    if SPONSORBLOCK == "true":
        q["sponsorblock"] = "all"

    try:
        with httpx.Client(timeout=60) as cx:
            r = cx.get(YTDLP_REMOTE_URL, params=q)
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
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _remote_ytdlp_dump(url) if YTDLP_MODE == "remote" else _local_ytdlp_dump(url)

    try:
        cache_set(ck, json.dumps(info))
    except Exception:
        pass
    return info

# ---------- header helpers ----------
def _yt_headers(info: dict) -> Dict[str, str]:
    """Headers yt-dlp suggests for fetching media."""
    hdrs = {}
    src = info.get("http_headers") or {}
    # Copy as-is (yt-dlp already excludes hop-by-hop headers)
    for k, v in src.items():
        if isinstance(k, str) and isinstance(v, str):
            hdrs[k] = v
    # Provide sane fallbacks if missing
    hdrs.setdefault("User-Agent", "Mozilla/5.0")
    hdrs.setdefault("Accept", "*/*")
    hdrs.setdefault("Accept-Language", "en-US,en;q=0.9")
    return hdrs

def _merge(*ds: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for d in ds:
        for k, v in (d or {}).items():
            if v is not None:
                out[k] = v
    return out

# ---------- format helpers ----------
def _fmt_is_video_only(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    return (v not in ("", "none")) and (a in ("", "none"))

def _fmt_is_audio_only(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    ext = (f.get("ext") or "").lower()
    audioish = (
        (a not in ("", "none"))
        or ext in ("m4a", "webm", "mp3", "opus")
        or bool(f.get("abr")) or bool(f.get("asr")) or bool(f.get("audio_channels"))
    )
    return (v in ("", "none")) and audioish

def _fmt_is_muxed(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    return (v not in ("", "none")) and (a not in ("", "none"))

def _is_mp4_audio(f: dict) -> bool:
    a = (f.get("acodec") or "").lower()
    ext = (f.get("ext") or "").lower()
    return ("mp4a" in a) or ("aac" in a) or (ext == "m4a")

def _best_audio(fmts: list[dict]) -> dict | None:
    auds = [f for f in fmts if _fmt_is_audio_only(f) and f.get("url")]
    if auds:
        return max(
            auds,
            key=lambda f: (1 if _is_mp4_audio(f) else 0, f.get("abr") or 0, f.get("tbr") or 0)
        )
    muxeds = [f for f in fmts if _fmt_is_muxed(f) and f.get("url")]
    if muxeds:
        def score(f):
            ext = (f.get("ext") or f.get("container") or "").lower()
            a = (f.get("acodec") or "").lower()
            mp4ish = 1 if (ext == "mp4") else 0
            aacish = 1 if (("mp4a" in a) or ("aac" in a)) else 0
            return (mp4ish + aacish, f.get("tbr") or 0)
        return max(muxeds, key=score)
    return None

def _map_formats(info: dict):
    out = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        fid = str(f.get("format_id") or f.get("itag") or "")
        if fid.startswith("sb"):  # storyboard entries
            continue
        has_v = _fmt_is_video_only(f) or _fmt_is_muxed(f)
        has_a = _fmt_is_audio_only(f) or _fmt_is_muxed(f)
        out.append({
            "itag": fid,
            "ext": f.get("ext") or f.get("container"),
            "vcodec": f.get("vcodec") or "none",
            "acodec": f.get("acodec") or "none",
            "height": f.get("height"),
            "tbr": f.get("tbr"),
            "quality_label": f.get("quality_label") or f.get("format_note"),
            "has_video": has_v,
            "has_audio": has_a,
        })
    return out

# ---------- selection ----------
def pick_stream(info: dict, policy: str = "h264_mp4") -> dict | None:
    fmts = info.get("formats") or []
    best = None
    if policy == "h264_mp4":
        mp4s = [f for f in fmts if (f.get("container") == "mp4" or f.get("ext") == "mp4")
                and _fmt_is_muxed(f) and f.get("url")]
        if mp4s:
            best = max(mp4s, key=lambda f: f.get("tbr") or 0)
    if not best:
        muxed = [f for f in fmts if _fmt_is_muxed(f) and f.get("url")]
        if muxed:
            best = max(muxed, key=lambda f: f.get("tbr") or 0)
    if not best:
        return None
    container = best.get("container") or best.get("ext") or "mp4"
    v = best.get("vcodec") or ""
    a = best.get("acodec") or ""
    return {"kind": "muxed", "url": best["url"], "container": container, "codecs": f"{v}+{a}".strip("+")}

def _pick_by_itag(info: dict, itag: str | None) -> dict | None:
    if not itag:
        return None
    fmts = info.get("formats") or []
    target = next((f for f in fmts
                   if str(f.get("format_id") or f.get("itag")) == str(itag) and f.get("url")), None)
    if not target:
        return None
    if _fmt_is_muxed(target):
        container = target.get("container") or target.get("ext") or "mp4"
        v = target.get("vcodec") or ""
        a = target.get("acodec") or ""
        return {"kind": "muxed", "url": target["url"], "container": container, "codecs": f"{v}+{a}".strip("+")}
    if _fmt_is_video_only(target):
        abest = _best_audio(fmts)
        if abest:
            return {"kind": "split", "container": "mp4", "video_url": target["url"], "audio_url": abest["url"]}
        return None
    if _fmt_is_audio_only(target):
        vids = [f for f in fmts if _fmt_is_video_only(f) and f.get("url")]
        if vids:
            vbest = max(vids, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
            return {"kind": "split", "container": "mp4", "video_url": vbest["url"], "audio_url": target["url"]}
        return None
    return None

# ---------- HTTP helpers ----------
async def backend_get(path: str, params: dict | None = None) -> httpx.Response:
    url = f"{BACKEND_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.get(url, params=params)
        return r

async def probe_headers(target_url: str, headers: Dict[str, str]):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cx:
        return await cx.head(target_url, headers=headers)

def _headers_kv(headers: dict) -> list[str]:
    kv = []
    for k, v in (headers or {}).items():
        kv += ["-headers", f"{k}: {v}\r\n"]
    return kv

# ---------- Routes ----------
@app.get("/healthz")
async def healthz():
    cookies_meta = {"enabled": bool(COOKIES), "path": COOKIES or None, "size": None}
    if COOKIES and os.path.exists(COOKIES):
        try:
            cookies_meta["size"] = os.path.getsize(COOKIES)
        except Exception:
            cookies_meta["size"] = None
    return JSONResponse({
        "ok": True,
        "ytdlp_mode": YTDLP_MODE,
        "ytdlp_cmd": YTDLP_CMD,
        "remote": YTDLP_REMOTE_URL or None,
        "ffmpeg_cmd": FFMPEG_CMD,
        "data_dir": DATA_DIR,
        "cookies": cookies_meta
    })

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

@app.get("/formats/{video_id}")
def list_formats(video_id: str):
    info = ytdlp_dump(video_id)
    fmts = _map_formats(info)
    fmts.sort(
        key=lambda x: (1 if x.get("has_video") else 0, x.get("height") or 0, x.get("tbr") or 0),
        reverse=True
    )
    return {"id": video_id, "title": info.get("title"), "formats": fmts}

@app.get("/resolve")
async def resolve(video_id: str, policy: str = "h264_mp4", itag: str | None = None):
    info = ytdlp_dump(video_id)
    stream = _pick_by_itag(info, itag) if itag else pick_stream(info, policy)
    if not stream:
        raise HTTPException(502, "No playable stream found")
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

@app.get("/play/{video_id}")
async def play(video_id: str, request: Request, policy: str = "h264_mp4", itag: str | None = None):
    info = ytdlp_dump(video_id)
    stream = _pick_by_itag(info, itag) if itag else pick_stream(info, policy)
    if not stream:
        raise HTTPException(502, "No playable stream (progressive or split) found")

    # Progressive (muxed) → proxy with ranges
    if stream.get("kind") == "muxed" and "url" in stream:
        target = stream["url"]

        passthru = {}
        if request.headers.get("Range"):    passthru["Range"]    = request.headers["Range"]
        if request.headers.get("If-Range"): passthru["If-Range"] = request.headers["If-Range"]
        hdrs = _merge(_yt_headers(info), passthru)

        async def generator(target_url: str, hdrs: Dict[str, str]):
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as cx:
                attempt = 0
                current_url = target_url
                current_hdrs = hdrs
                while True:
                    async with cx.stream("GET", current_url, headers=current_hdrs) as resp:
                        # If expired/forbidden, refresh once via yt-dlp
                        if resp.status_code in (403, 410) and attempt == 0:
                            attempt += 1
                            info3 = ytdlp_dump(video_id)
                            stream3 = _pick_by_itag(info3, itag) if itag else pick_stream(info3, policy)
                            if not (stream3 and stream3.get("kind") == "muxed" and "url" in stream3):
                                raise HTTPException(502, f"Upstream refused playback ({resp.status_code})")
                            current_url = stream3["url"]
                            current_hdrs = _merge(_yt_headers(info3), passthru)
                            continue
                        if resp.status_code not in (200, 206):
                            raise HTTPException(resp.status_code, f"upstream status {resp.status_code}")
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                        break  # normal EOF

        # Try an upstream HEAD to mirror useful headers
        try:
            hr = await probe_headers(target, hdrs)
        except Exception:
            hr = None

        # If expired, refresh once for header probe too
        if hr is not None and hr.status_code in (403, 410):
            info2 = ytdlp_dump(video_id)
            stream2 = _pick_by_itag(info2, itag) if itag else pick_stream(info2, policy)
            if stream2 and stream2.get("kind") == "muxed" and "url" in stream2:
                target = stream2["url"]
                hdrs = _merge(_yt_headers(info2), passthru)
                try:
                    hr = await probe_headers(target, hdrs)
                except Exception:
                    hr = None

        resp_headers, status = {}, 200
        if hr is not None:
            status = 206 if hdrs.get("Range") and hr.status_code in (200, 206) else 200
            for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Last-Modified", "ETag"]:
                if h in hr.headers:
                    resp_headers[h] = hr.headers[h]
        resp_headers.setdefault("Accept-Ranges", "bytes")

        return StreamingResponse(generator(target, hdrs), status_code=status, headers=resp_headers)

    # Split (video-only + audio-only) → live remux (no ranges)
    if stream.get("kind") == "split" and stream.get("video_url") and stream.get("audio_url"):
        v = stream["video_url"]
        a = stream["audio_url"]

        yt_hdrs = _yt_headers(info)
        cmd = [
            FFMPEG_CMD, "-loglevel", "error", "-nostdin", "-hide_banner",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
            *(_headers_kv(yt_hdrs)), "-i", v,
            *(_headers_kv(yt_hdrs)), "-i", a,
            "-c", "copy",
            "-movflags", "+frag_keyframe+empty_moov",
            "-f", "mp4", "pipe:1",
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError:
            raise HTTPException(500, f"ffmpeg not found at '{FFMPEG_CMD}'. Set FFMPEG_CMD or install ffmpeg.")

        async def gen():
            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    rc = proc.poll()
                    if rc not in (0, None):
                        try:
                            err = (proc.stderr.read() or b"").decode("utf-8", "ignore")
                        except Exception:
                            err = ""
                        raise HTTPException(status_code=502, detail=f"ffmpeg failed: {err.strip()[:500]}")
                finally:
                    try: proc.kill()
                    except: pass

        return StreamingResponse(gen(), media_type="video/mp4", headers={"Accept-Ranges": "none"})

    raise HTTPException(502, "No playable stream (progressive or split) found")

@app.head("/play/{video_id}")
async def play_head(video_id: str, request: Request, policy: str = "h264_mp4", itag: str | None = None):
    """Support HEAD (best effort). Split-remux returns generic headers."""
    info = ytdlp_dump(video_id)
    stream = _pick_by_itag(info, itag) if itag else pick_stream(info, policy)

    # Progressive → proxy upstream headers (fallback to tiny GET if needed)
    if stream and stream.get("kind") == "muxed" and "url" in stream:
        target  = stream["url"]
        yt_hdrs = _yt_headers(info)
        passthru = {}
        if request.headers.get("Range"):    passthru["Range"]    = request.headers["Range"]
        if request.headers.get("If-Range"): passthru["If-Range"] = request.headers["If-Range"]
        headers = _merge(yt_hdrs, passthru)

        hr = None
        try:
            hr = await probe_headers(target, headers)
        except Exception:
            hr = None

        # Fallback: some hosts omit Content-Range on HEAD, use a tiny ranged GET
        if headers.get("Range") and (hr is None or ("Content-Range" not in hr.headers)):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cx:
                    async with cx.stream("GET", target, headers=headers) as gr:
                        await gr.aclose()
                        class _H:
                            status_code = gr.status_code
                            headers = gr.headers
                        hr = _H
            except Exception:
                pass

        resp_headers, status = {}, 200
        if hr is not None:
            if headers.get("Range") and (hr.status_code in (200, 206)):
                status = 206 if ("Content-Range" in hr.headers) else 200
            for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Last-Modified", "ETag"]:
                if h in hr.headers:
                    resp_headers[h] = hr.headers[h]
        resp_headers.setdefault("Accept-Ranges", "bytes")
        return Response(status_code=status, headers=resp_headers)

    # Split remux: unknown size; generic OK
    return Response(status_code=200, headers={"Content-Type": "video/mp4"})

# ---------- Subscriptions / Favorites API ----------
@app.get("/subscriptions", response_class=JSONResponse)
def get_subscriptions():
    return load_subscriptions()

@app.get("/favorites", response_class=JSONResponse)
def get_favorites():
    return load_favorites()

@app.post("/subscriptions/import")
async def import_subscriptions(format: str = "auto", file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="ignore")
    if format == "opml" or (format == "auto" and raw.lstrip().startswith("<")):
        new_items = parse_opml_to_subs(raw)
    else:
        try:
            obj = json.loads(raw)
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        new_items = parse_json_to_subs(obj)

    if not new_items:
        raise HTTPException(400, "No subscriptions found")
    current = load_subscriptions()
    merged = current + new_items
    save_subscriptions(merged)
    return {"imported": len(new_items), "total": len(load_subscriptions())}

@app.get("/subscriptions/export")
def export_subscriptions(format: str = "opml"):
    subs = load_subscriptions()
    if format == "opml":
        text = _opml_for_subs(subs)
        return Response(
            content=text,
            media_type="text/xml",
            headers={"Content-Disposition": 'attachment; filename="jellytube_subscriptions.opml"'}
        )
    elif format in ("freetube", "json"):
        payload = {"subscriptions": [{"channelId": s["channelId"], "name": s.get("title")} for s in subs]}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            content=text,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="jellytube_subscriptions.json"'}
        )
    else:
        raise HTTPException(400, "format must be opml|freetube|json")

def _extract_channel_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:channel_id=|/channel/)(UC[0-9A-Za-z_-]{22})", url)
    if m:
        return m.group(1)
    return None

def parse_opml_to_subs(text: str) -> List[Dict[str, Any]]:
    import xml.etree.ElementTree as ET
    subs: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        for node in root.iter("outline"):
            title = node.attrib.get("title") or node.attrib.get("text")
            xmlUrl = node.attrib.get("xmlUrl") or ""
            htmlUrl = node.attrib.get("htmlUrl") or ""
            cid = _extract_channel_id_from_url(xmlUrl) or _extract_channel_id_from_url(htmlUrl)
            if cid:
                subs.append({"channelId": cid, "title": title, "url": htmlUrl or xmlUrl})
    except Exception:
        pass
    return subs

def parse_json_to_subs(obj: Any) -> List[Dict[str, Any]]:
    subs: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        if isinstance(obj.get("subscriptions"), list):
            items = obj["subscriptions"]
        elif isinstance(obj.get("channels"), list):
            items = obj["channels"]
        elif isinstance(obj.get("data"), dict) and isinstance(obj["data"].get("subscriptions"), list):
            items = obj["data"]["subscriptions"]
    elif isinstance(obj, list):
        items = obj

    for s in items:
        cid = s.get("channelId") or s.get("authorId") or s.get("id")
        url = s.get("url") or s.get("channelUrl") or s.get("link")
        if not cid and isinstance(url, str):
            cid = _extract_channel_id_from_url(url)
        if cid:
            subs.append({"channelId": cid, "title": s.get("name") or s.get("author") or s.get("title"), "url": url})
    return subs

def parse_json_to_favs(obj: Any) -> List[Dict[str, Any]]:
    favs: List[Dict[str, Any]] = []
    def add(vid, title=None):
        if vid:
            favs.append({"videoId": vid, "title": title})
    if isinstance(obj, dict):
        for key in ("favorites", "bookmarks", "watchLater", "liked"):
            val = obj.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        add(it.get("videoId") or it.get("id"), it.get("title"))
                    elif isinstance(it, str):
                        add(it)
        if isinstance(obj.get("playlists"), list):
            for pl in obj["playlists"]:
                for it in pl.get("videos") or []:
                    if isinstance(it, dict):
                        add(it.get("videoId") or it.get("id"), it.get("title"))
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                add(it.get("videoId") or it.get("id"), it.get("title"))
            elif isinstance(it, str):
                add(it)
    return favs

def _opml_for_subs(subs: List[Dict[str, Any]]) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="1.0">',
        '  <head>',
        f'    <title>JellyTube Subscriptions ({now})</title>',
        '  </head>',
        '  <body>',
    ]
    for s in subs:
        cid = s["channelId"]
        title = (s.get("title") or cid).replace('"', "'")
        html = s.get("url") or f"https://www.youtube.com/channel/{cid}"
        xmlu = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        lines.append(f'    <outline text="{title}" title="{title}" type="rss" xmlUrl="{xmlu}" htmlUrl="{html}" />')
    lines += ['  </body>', '</opml>']
    return "\n".join(lines)

@app.post("/favorites/import")
async def import_favorites(file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="ignore")
    try:
        obj = json.loads(raw)
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    new_items = parse_json_to_favs(obj)
    if not new_items:
        raise HTTPException(400, "No favorites found")
    current = load_favorites()
    merged = current + new_items
    save_favorites(merged)
    return {"imported": len(new_items), "total": len(load_favorites())}

@app.get("/favorites/export")
def export_favorites():
    favs = load_favorites()
    text = json.dumps({"favorites": favs}, ensure_ascii=False, indent=2)
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="jellytube_favorites.json"'}
    )

@app.post("/favorites/add")
async def add_favorite(video_id: str = Form(...), title: str | None = Form(None)):
    favs = load_favorites()
    favs.append({"videoId": video_id, "title": title})
    save_favorites(favs)
    return {"ok": True, "total": len(load_favorites())}
