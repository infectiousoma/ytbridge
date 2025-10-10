# src/format_utils.py
import re
from typing import Any, Dict, List

# ---------------------- map_formats & helpers ----------------------

def _to_int(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        s = str(v).strip().lower()
        if s.endswith("p") and s[:-1].isdigit():
            return int(s[:-1])
        return int(float(s))
    except Exception:
        return default

def _to_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v).strip())
    except Exception:
        return default

def _has_video(fmt: Dict[str, Any]) -> bool:
    v = (fmt.get("vcodec") or "").lower()
    if v and v != "none":
        return True
    # some extractors omit vcodec; height/fps implies video
    return bool(fmt.get("height") or fmt.get("fps"))

def _has_audio(fmt: Dict[str, Any]) -> bool:
    a = (fmt.get("acodec") or "").lower()
    if a and a != "none":
        return True
    return bool(fmt.get("abr") or fmt.get("audio_ext"))

def _is_storyboard(fmt: Dict[str, Any]) -> bool:
    note = (fmt.get("format_note") or "").strip().lower()
    proto = (fmt.get("protocol") or "").strip().lower()
    ext = (fmt.get("ext") or "").strip().lower()
    if proto == "mhtml" or ext == "mhtml":
        return True
    return "storyboard" in note or "preview" in note

def _quality_label(fmt: Dict[str, Any]) -> str | None:
    ql = fmt.get("quality_label")
    if ql:
        return ql
    h = _to_int(fmt.get("height"))
    return f"{h}p" if h else None

def map_formats(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize yt-dlp formats for /formats and the Jellyfin plugin.
    Guarantees: has_video/has_audio booleans, sane itag/ext/height/tbr,
    filters out storyboard/preview entries, preserves progressive vs split via has_* flags.
    """
    formats = info.get("formats") or []
    out: List[Dict[str, Any]] = []

    for f in formats:
        if _is_storyboard(f):
            continue

        itag = str(f.get("format_id") or f.get("itag") or "").strip()
        if not itag:
            continue

        url = f.get("url")
        has_v = _has_video(f)
        has_a = _has_audio(f)

        height = _to_int(f.get("height"))
        if height is None:
            res = f.get("resolution")
            if isinstance(res, str):
                # fast parse: take the right side of last 'x'
                x = res.rfind("x")
                if x != -1:
                    height = _to_int(res[x + 1:])

        tbr = _to_float(f.get("tbr"))
        if tbr is None:
            vb = _to_float(f.get("vbr"), 0.0) or 0.0
            ab = _to_float(f.get("abr"), 0.0) or 0.0
            tbr = (vb + ab) if (vb or ab) else None

        item = {
            "itag": itag,
            "ext": (f.get("ext") or "").lower() or None,
            "has_video": bool(has_v),
            "has_audio": bool(has_a),
            "vcodec": f.get("vcodec") or None,
            "acodec": f.get("acodec") or None,
            "height": height,
            "tbr": tbr,
            "quality_label": _quality_label(f),
            "url": url,
        }
        out.append(item)

    return out

# ---------------------- request header helpers ----------------------

_DEFAULT_YT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

def yt_headers(info: dict | None = None) -> dict:
    """
    Build request headers for fetching googlevideo.com URLs.
    - Prefer headers emitted by yt-dlp when present.
    - Fall back to a sane desktop Chrome UA.
    """
    h = {}
    try:
        top = (info or {}).get("http_headers") or {}
        if isinstance(top, dict):
            h.update(top)
    except Exception:
        pass

    h.setdefault("User-Agent", _DEFAULT_YT_UA)
    h.setdefault("Accept", "*/*")
    h.setdefault("Connection", "keep-alive")
    return h

def merge_headers(base: dict | None, extra: dict | None) -> dict:
    """
    Shallow-merge HTTP headers with case-preserving keys.
    Caller wins for duplicates (extra overrides base).
    """
    out = {}
    if isinstance(base, dict):
        out.update(base)
    if isinstance(extra, dict):
        out.update(extra)
    return out

# --- legacy compatibility helpers for select_utils.py ---

def _has_video_any(fmt: dict) -> bool:
    # Prefer normalized flags if present, otherwise infer from raw fields
    if "has_video" in fmt:
        return bool(fmt.get("has_video"))
    return _has_video(fmt)  # uses the helper already in this module

def _has_audio_any(fmt: dict) -> bool:
    if "has_audio" in fmt:
        return bool(fmt.get("has_audio"))
    return _has_audio(fmt)  # uses the helper already in this module

def fmt_is_muxed(fmt: dict) -> bool:
    """True if the format carries both audio and video."""
    return _has_video_any(fmt) and _has_audio_any(fmt)

def fmt_is_video_only(fmt: dict) -> bool:
    """True if the format has video but no audio."""
    return _has_video_any(fmt) and not _has_audio_any(fmt)

def fmt_is_audio_only(fmt: dict) -> bool:
    """True if the format has audio but no video."""
    return _has_audio_any(fmt) and not _has_video_any(fmt)

def best_audio(formats: list[dict]) -> dict | None:
    """
    Pick the highest-bitrate audio-only candidate.
    Works for both raw yt-dlp formats and normalized map_formats output.
    """
    best = None
    best_key = -1.0
    for f in formats or []:
        if not fmt_is_audio_only(f):
            continue
        # Prefer tbr; fall back to abr
        t = f.get("tbr")
        if t is None:
            t = f.get("abr")
        try:
            key = float(t) if t is not None else -1.0
        except Exception:
            key = -1.0
        if key > best_key:
            best_key = key
            best = f
    return best
