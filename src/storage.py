import json, os, time, re
from typing import Any, Dict, List
from . import config

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
    data = _load_list(config.SUBS_PATH)
    return data if isinstance(data, list) else []

def save_subscriptions(items: List[Dict[str, Any]]):
    seen, out = set(), []
    for it in items:
        cid = it.get("channelId") or it.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"channelId": cid, "title": it.get("title"), "url": it.get("url")})
    _save_list(config.SUBS_PATH, out)

def load_favorites() -> List[Dict[str, Any]]:
    data = _load_list(config.FAVS_PATH)
    return data if isinstance(data, list) else []

def save_favorites(items: List[Dict[str, Any]]):
    seen, out = set(), []
    for it in items:
        vid = it.get("videoId") or it.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append({"videoId": vid, "title": it.get("title")})
    _save_list(config.FAVS_PATH, out)

def _extract_channel_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:channel_id=|/channel/)(UC[0-9A-Za-z_-]{22})", url)
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

def opml_for_subs(subs: List[Dict[str, Any]]) -> str:
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
