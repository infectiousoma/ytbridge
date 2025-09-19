from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
import json

from ..storage import (
    load_subscriptions, save_subscriptions, parse_opml_to_subs, parse_json_to_subs,
    load_favorites, save_favorites, parse_json_to_favs, opml_for_subs
)

router = APIRouter()

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
