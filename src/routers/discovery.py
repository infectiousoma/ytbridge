from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
import json, os

from .. import config
from ..cache import cache_get, cache_set
from ..http_utils import backend_get
from ..ytdlp_adapter import ytdlp_dump
from ..format_utils import map_formats

router = APIRouter()

@router.get("/healthz")
async def healthz():
    cookies_meta = {"enabled": bool(config.COOKIES), "path": config.COOKIES or None, "size": None}
    try:
        if config.COOKIES and os.path.exists(config.COOKIES):
            cookies_meta["size"] = os.path.getsize(config.COOKIES)
    except Exception:
        cookies_meta["size"] = None
    return JSONResponse({
        "ok": True,
        "ytdlp_mode": config.YTDLP_MODE,
        "ytdlp_cmd": config.YTDLP_CMD,
        "remote": config.YTDLP_REMOTE_URL or None,
        "ffmpeg_cmd": config.FFMPEG_CMD,
        "data_dir": config.DATA_DIR,
        "cookies": cookies_meta
    })

@router.get("/search")
async def search(q: str, type: str = "video", page: int = 1, limit: int = 30):
    type_map = {"video": "video", "channel": "channel", "playlist": "playlist"}
    if type not in type_map:
        raise HTTPException(400, "Invalid type")
    if config.BACKEND_PROVIDER == "invidious":
        r = await backend_get("/api/v1/search", {"q": q, "page": page, "type": type_map[type]})
    elif config.BACKEND_PROVIDER == "piped":
        r = await backend_get("/api/v1/search", {"q": q})
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream search error: {r.text[:200]}")
    data = r.json()
    if isinstance(data, list):
        data = data[:limit]
    return JSONResponse(data)

@router.get("/channel/{channel_id}")
async def channel(channel_id: str, page: int = 1):
    if config.BACKEND_PROVIDER == "invidious":
        r = await backend_get(f"/api/v1/channels/{channel_id}/videos", {"page": page})
    elif config.BACKEND_PROVIDER == "piped":
        r = await backend_get(f"/api/v1/channel/{channel_id}")
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream channel error: {r.text[:200]}")
    return JSONResponse(r.json())

@router.get("/item/{video_id}")
async def item(video_id: str):
    ckey = f"meta:item:{video_id}"
    cached = cache_get(ckey)
    if cached:
        try:
            return JSONResponse(json.loads(cached))
        except Exception:
            pass

    if config.BACKEND_PROVIDER == "invidious":
        r = await backend_get(f"/api/v1/videos/{video_id}")
    elif config.BACKEND_PROVIDER == "piped":
        r = await backend_get(f"/api/v1/video/{video_id}")
    else:
        raise HTTPException(500, "Unsupported BACKEND_PROVIDER")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Upstream item error: {r.text[:200]}")
    meta = r.json()

    # Enrich with yt-dlp data, but don't fail the endpoint if yt-dlp errors
    try:
        info = ytdlp_dump(video_id)
        meta["chapters"]  = info.get("chapters") or []
        meta["subtitles"] = info.get("subtitles") or {}
        meta["duration"]  = info.get("duration") or meta.get("lengthSeconds")
        thumbs = info.get("thumbnails") or []
        if thumbs:
            meta["thumbnails"] = thumbs
    except HTTPException as e:
        meta["_ytdlp_error"] = getattr(e, "detail", str(e))

    try:
        cache_set(ckey, json.dumps(meta))
    except Exception:
        pass
    return JSONResponse(meta)

@router.get("/formats/{video_id}")
def list_formats(video_id: str, debug: bool = False):
    try:
        info = ytdlp_dump(video_id)
        fmts = map_formats(info)
        fmts.sort(
            key=lambda x: (1 if x.get("has_video") else 0, x.get("height") or 0, x.get("tbr") or 0),
            reverse=True
        )
        payload = {"id": video_id, "title": info.get("title"), "formats": fmts}
        if debug:
            payload["_raw_extractors"] = {
                "extractor": info.get("extractor"),
                "webpage_url": info.get("webpage_url")
            }
        return payload
    except HTTPException as e:
        code = getattr(e, "status_code", 500) or 500
        detail = getattr(e, "detail", str(e))
        raise HTTPException(code, f"yt-dlp failed for {video_id}: {detail}")

@router.get("/diag/yt-dlp")
def diag_ytdlp(video_id: str):
    try:
        info = ytdlp_dump(video_id)
        return {
            "ok": True,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "extractor": info.get("extractor"),
            "n_formats": len(info.get("formats") or [])
        }
    except HTTPException as e:
        return {"ok": False, "error": getattr(e, "detail", str(e))}
