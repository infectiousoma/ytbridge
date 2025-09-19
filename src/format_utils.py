from typing import Dict

def yt_headers(info: dict) -> Dict[str, str]:
    hdrs = {}
    src = info.get("http_headers") or {}
    for k, v in src.items():
        if isinstance(k, str) and isinstance(v, str):
            hdrs[k] = v
    hdrs.setdefault("User-Agent", "Mozilla/5.0")
    hdrs.setdefault("Accept", "*/*")
    hdrs.setdefault("Accept-Language", "en-US,en;q=0.9")
    return hdrs

def merge_headers(*ds: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for d in ds:
        for k, v in (d or {}).items():
            if v is not None:
                out[k] = v
    return out

def fmt_is_video_only(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    return (v not in ("", "none")) and (a in ("", "none"))

def fmt_is_audio_only(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    ext = (f.get("ext") or "").lower()
    audioish = (
        (a not in ("", "none"))
        or ext in ("m4a", "webm", "mp3", "opus")
        or bool(f.get("abr")) or bool(f.get("asr")) or bool(f.get("audio_channels"))
    )
    return (v in ("", "none")) and audioish

def fmt_is_muxed(f: dict) -> bool:
    v = (f.get("vcodec") or "").lower()
    a = (f.get("acodec") or "").lower()
    return (v not in ("", "none")) and (a not in ("", "none"))

def is_mp4_audio(f: dict) -> bool:
    a = (f.get("acodec") or "").lower()
    ext = (f.get("ext") or "").lower()
    return ("mp4a" in a) or ("aac" in a) or (ext == "m4a")

def best_audio(fmts: list[dict]) -> dict | None:
    auds = [f for f in fmts if fmt_is_audio_only(f) and f.get("url")]
    if auds:
        return max(
            auds,
            key=lambda f: (1 if is_mp4_audio(f) else 0, f.get("abr") or 0, f.get("tbr") or 0)
        )
    muxeds = [f for f in fmts if fmt_is_muxed(f) and f.get("url")]
    if muxeds:
        def score(f):
            ext = (f.get("ext") or f.get("container") or "").lower()
            a = (f.get("acodec") or "").lower()
            mp4ish = 1 if (ext == "mp4") else 0
            aacish = 1 if (("mp4a" in a) or ("aac" in a)) else 0
            return (mp4ish + aacish, f.get("tbr") or 0)
        return max(muxeds, key=score)
    return None

def map_formats(info: dict):
    out = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        fid = str(f.get("format_id") or f.get("itag") or "")
        if fid.startswith("sb"):
            continue
        has_v = fmt_is_video_only(f) or fmt_is_muxed(f)
        has_a = fmt_is_audio_only(f) or fmt_is_muxed(f)
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
