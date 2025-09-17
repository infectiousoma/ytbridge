# yt_bridge.py
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import os, subprocess, json, urllib.parse, httpx, redis, re, pathlib, time
from typing import List, Dict, Any, Tuple, Optional

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

# Persistent data (favorites/subscriptions)
DATA_DIR  = os.environ.get("DATA_DIR", "/data")
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
SUBS_PATH = os.path.join(DATA_DIR, "subscriptions.json")
FAVS_PATH = os.path.join(DATA_DIR, "favorites.json")

app = FastAPI(title="ytbridge", version="0.5.0")
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
        out.append({"channelId": cid, "title": it.get("title"), "url": it.get("url"), "handle": it.get("handle")})
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

# ---------- yt-dlp adapters (sync) ----------
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

def _probe_url(url: str) -> dict:
    return _remote_ytdlp_dump(url) if YTDLP_MODE == "remote" else _local_ytdlp_dump(url)

def ytdlp_dump(video_id: str) -> dict:
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _probe_url(url)

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

# ---------- Import/Export helpers & resolvers ----------
_CH_RE = re.compile(r'^UC[0-9A-Za-z_-]{22}$')

def _extract_channel_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    # direct /channel/UCxxxx or feeds url
    m = re.search(r"(?:channel_id=|/channel/)(UC[0-9A-Za-z_-]{22})", url)
    if m:
        return m.group(1)
    return None

def _extract_handle_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/@([A-Za-z0-9_.-]+)", url)
    if m:
        return '@' + m.group(1)
    return None

def _parse_vanity_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/(?:user|c)/([^/?#]+)", url)
    return m.group(1) if m else None

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
            handle = _extract_handle_from_url(htmlUrl) or _extract_handle_from_url(xmlUrl)
            vanity = _parse_vanity_from_url(htmlUrl) or _parse_vanity_from_url(xmlUrl)
            subs.append({"channelId": cid, "title": title, "url": htmlUrl or xmlUrl, "handle": handle, "vanity": vanity})
    except Exception:
        pass
    # keep entries even if channelId is None; we'll resolve them below
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
        url = s.get("url") or s.get("channelUrl") or s.get("link") or ""
        cid = s.get("channelId") or s.get("authorId") or s.get("id") or _extract_channel_id_from_url(url)
        handle = s.get("handle") or _extract_handle_from_url(url)
        vanity = _parse_vanity_from_url(url)
        subs.append({
            "channelId": cid,
            "title": s.get("name") or s.get("author") or s.get("title"),
            "url": url,
            "handle": handle,
            "vanity": vanity
        })
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

# ---------- Channel resolvers (sync) ----------
def _search_channel_invidious(query: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with httpx.Client(timeout=30) as cx:
            r = cx.get(f"{BACKEND_BASE}/api/v1/search", params={"q": query, "type": "channel"})
            if r.status_code == 200:
                js = r.json()
                if isinstance(js, list) and js:
                    cid = js[0].get("authorId") or js[0].get("channelId")
                    title = js[0].get("author") or js[0].get("authorName") or js[0].get("name")
                    if cid and _CH_RE.match(cid):
                        return cid, title
    except Exception:
        pass
    return None, None

def _resolve_channel_id(spec: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    spec may be UC..., @handle, vanity name, or any youtube channel URL.
    Returns (channelId, meta)
    """
    spec = (spec or "").strip()
    meta: Dict[str, Any] = {}

    if not spec:
        return None, meta

    if _CH_RE.match(spec):
        return spec, meta

    # URL forms
    if spec.startswith("http://") or spec.startswith("https://"):
        cid = _extract_channel_id_from_url(spec)
        if cid:
            return cid, meta

        handle = _extract_handle_from_url(spec)
        if handle:
            meta["handle"] = handle
            if BACKEND_PROVIDER == "invidious":
                cid, title = _search_channel_invidious(handle)
                if cid:
                    if title: meta["title"] = title
                    return cid, meta
            # yt-dlp fallback
            try:
                info = _probe_url(f"https://www.youtube.com/{handle}")
                cid = info.get("channel_id") or info.get("uploader_id")
                title = info.get("channel") or info.get("uploader")
                if cid and _CH_RE.match(cid):
                    if title: meta["title"] = title
                    return cid, meta
            except HTTPException:
                pass

        vanity = _parse_vanity_from_url(spec)
        if vanity:
            q = vanity
            if BACKEND_PROVIDER == "invidious":
                cid, title = _search_channel_invidious(q)
                if cid:
                    if title: meta["title"] = title
                    return cid, meta
            try:
                info = _probe_url(spec)
                cid = info.get("channel_id") or info.get("uploader_id")
                title = info.get("channel") or info.get("uploader")
                if cid and _CH_RE.match(cid):
                    if title: meta["title"] = title
                    return cid, meta
            except HTTPException:
                pass

    # @handle without scheme
    if spec.startswith('@'):
        if BACKEND_PROVIDER == "invidious":
            cid, title = _search_channel_invidious(spec)
            if cid:
                if title: meta["title"] = title
                meta["handle"] = spec
                return cid, meta
        try:
            info = _probe_url(f"https://www.youtube.com/{spec}")
            cid = info.get("channel_id") or info.get("uploader_id")
            title = info.get("channel") or info.get("uploader")
            if cid and _CH_RE.match(cid):
                if title: meta["title"] = title
                meta["handle"] = spec
                return cid, meta
        except HTTPException:
            pass

    # last resort: try Invidious search by plain text
    if BACKEND_PROVIDER == "invidious":
        cid, title = _search_channel_invidious(spec)
        if cid:
            if title: meta["title"] = title
            return cid, meta

    return None, meta

def _normalize_and_resolve_subs(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in entries:
        cid = s.get("channelId")
        title = s.get("title")
        url = s.get("url")
        handle = s.get("handle")
        vanity = s.get("vanity")
        if not cid:
            # Try URL/handle/vanity resolution
            spec = handle or url or vanity or title or ""
            rcid, meta = _resolve_channel_id(spec)
            cid = rcid
            # merge titles/handles if we learned something
            if meta.get("title") and not title:
                title = meta["title"]
            if meta.get("handle") and not handle:
                handle = meta["handle"]
        if cid and _CH_RE.match(cid):
            out.append({"channelId": cid, "title": title, "url": url, "handle": handle})
    # de-dupe
    seen, uniq = set(), []
    for it in out:
        cid = it["channelId"]
        if cid in seen:
            continue
        seen.add(cid)
        uniq.append(it)
    return uniq

# ---------- Routes ----------
@app.get("/healthz")
async def healthz():
    return JSONResponse({
        "ok": True,
        "ytdlp_mode": YTDLP_MODE,
        "ytdlp_cmd": YTDLP_CMD,
        "remote": YTDLP_REMOTE_URL or None,
        "data_dir": DATA_DIR
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

@app.head("/play/{video_id}")
async def play_head(video_id: str, request: Request, policy: str = "h264_mp4"):
    info = ytdlp_dump(video_id)
    stream = pick_stream(info, policy)
    target = stream["url"]

    headers = {}
    range_hdr = request.headers.get("Range")
    if range_hdr:
        headers["Range"] = range_hdr
    if request.headers.get("If-Range"):
        headers["If-Range"] = request.headers.get("If-Range")

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
    return Response(status_code=status, headers=resp_headers)

# ---------- Subscriptions / Favorites API ----------
@app.get("/subscriptions", response_class=JSONResponse)
def get_subscriptions():
    return load_subscriptions()

@app.post("/subscriptions/add")
def add_subscription(spec: str = Form(...), title: Optional[str] = Form(None)):
    """
    spec can be channelId (UC...), @handle, vanity, or any YouTube channel URL.
    """
    cid, meta = _resolve_channel_id(spec)
    if not cid:
        raise HTTPException(400, "Could not resolve channel")
    subs = load_subscriptions()
    subs.append({"channelId": cid, "title": title or meta.get("title"), "handle": meta.get("handle"),
                 "url": f"https://www.youtube.com/channel/{cid}"})
    save_subscriptions(subs)
    return {"ok": True, "channelId": cid, "total": len(load_subscriptions())}

@app.delete("/subscriptions/{channel_id}")
def delete_subscription(channel_id: str):
    subs = [s for s in load_subscriptions() if s.get("channelId") != channel_id]
    save_subscriptions(subs)
    return {"ok": True, "total": len(subs)}

@app.get("/favorites", response_class=JSONResponse)
def get_favorites():
    return load_favorites()

@app.delete("/favorites/{video_id}")
def delete_favorite(video_id: str):
    favs = [f for f in load_favorites() if f.get("videoId") != video_id]
    save_favorites(favs)
    return {"ok": True, "total": len(favs)}

@app.post("/subscriptions/import")
async def import_subscriptions(format: str = "auto", file: UploadFile = File(...)):
    raw = (await file.read()).decode("utf-8", errors="ignore")
    if format == "opml" or (format == "auto" and raw.lstrip().startswith("<")):
        entries = parse_opml_to_subs(raw)
    else:
        try:
            obj = json.loads(raw)
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        entries = parse_json_to_subs(obj)

    if not isinstance(entries, list) or not entries:
        raise HTTPException(400, "No subscriptions found")

    resolved = _normalize_and_resolve_subs(entries)
    if not resolved:
        raise HTTPException(400, "Could not resolve any channels")

    current = load_subscriptions()
    merged = current + resolved
    save_subscriptions(merged)
    return {"imported": len(resolved), "total": len(load_subscriptions())}

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
