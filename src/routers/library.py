# routers/library.py
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
import json

from ..storage import (
    load_subscriptions, save_subscriptions, parse_opml_to_subs, parse_json_to_subs,
    load_favorites, save_favorites, parse_json_to_favs, opml_for_subs
)
from ..ytdlp_adapter import ytdlp_dump

router = APIRouter()

# ---------- Subscriptions ----------
@router.get("/subscriptions", response_class=JSONResponse)
def get_subscriptions():
    return load_subscriptions()

@router.post("/subscriptions/import")
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

@router.get("/subscriptions/export")
def export_subscriptions(format: str = "opml"):
    subs = load_subscriptions()
    if format == "opml":
        text = opml_for_subs(subs)
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

# ---------- Favorites ----------
@router.get("/favorites", response_class=JSONResponse)
def get_favorites():
    return load_favorites()

@router.post("/favorites/import")
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

@router.get("/favorites/export")
def export_favorites():
    favs = load_favorites()
    text = json.dumps({"favorites": favs}, ensure_ascii=False, indent=2)
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="jellytube_favorites.json"'}
    )

@router.post("/favorites/add")
async def add_favorite(video_id: str = Form(...), title: str | None = Form(None)):
    favs = load_favorites()
    favs.append({"videoId": video_id, "title": title})
    save_favorites(favs)
    return {"ok": True, "total": len(load_favorites())}

# ---------- Formats (used by JellyTube plugin) ----------
@router.get("/formats/{video_id}", response_class=JSONResponse)
def get_formats(video_id: str):
    """
    Returns a slim format list compatible with the Jellyfin plugin:
    {
      "id": "...",
      "title": "...",
      "formats": [
        {
          "itag": "18", "ext": "mp4",
          "has_video": true, "has_audio": true,
          "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
          "height": 360, "tbr": 700.5, "quality_label": "360p"
        },
        ...
      ]
    }
    """
    info = ytdlp_dump(video_id)
    if not info or "formats" not in info:
        raise HTTPException(502, "yt-dlp returned no formats")

    out = {
        "id": info.get("id") or video_id,
        "title": info.get("title"),
        "formats": []
    }

    fmts = info.get("formats") or []
    for f in fmts:
        itag = f.get("format_id") or f.get("itag")
        if not itag:
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        has_video = (vcodec is not None and vcodec != "none")
        has_audio = (acodec is not None and acodec != "none")

        out["formats"].append({
            "itag": str(itag),
            "ext": f.get("ext"),
            "has_video": bool(has_video),
            "has_audio": bool(has_audio),
            "vcodec": vcodec,
            "acodec": acodec,
            "height": f.get("height"),
            "tbr": f.get("tbr"),
            "quality_label": f.get("format_note") or f.get("quality_label"),
        })

    # Small, deterministic sort: progressive (A+V) before video-only, then by height desc
    def _key(x):
        return (0 if (x["has_video"] and x["has_audio"]) else 1, -(x["height"] or 0))
    out["formats"].sort(key=_key)

    return out
