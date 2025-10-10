from typing import Dict, List, Optional, Any
from .format_utils import fmt_is_muxed, fmt_is_video_only, fmt_is_audio_only, best_audio

def _best_video(formats: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Pick the best video-only candidate by (height, tbr).
    Works with raw yt-dlp formats or normalized ones.
    """
    vids = [f for f in (formats or []) if fmt_is_video_only(f) and f.get("url")]
    if not vids:
        return None

    def _score(f):
        h = f.get("height") or 0
        t = f.get("tbr") or 0
        try:
            return (int(h), float(t))
        except Exception:
            return (int(h) if isinstance(h, int) else 0, 0.0)

    return max(vids, key=_score)


def _best_muxed(formats: List[Dict[str, Any]], ext_preference: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Pick the best muxed candidate; if ext_preference is set (e.g. "mp4"),
    prefer those first, otherwise take any muxed.
    """
    muxed_all = [f for f in (formats or []) if fmt_is_muxed(f) and f.get("url")]
    if not muxed_all:
        return None

    def _score(f):
        h = f.get("height") or 0
        t = f.get("tbr") or 0
        try:
            return (int(h), float(t))
        except Exception:
            return (int(h) if isinstance(h, int) else 0, 0.0)

    if ext_preference:
        pref = [f for f in muxed_all if (f.get("container") == ext_preference or f.get("ext") == ext_preference)]
        if pref:
            return max(pref, key=_score)

    return max(muxed_all, key=_score)


def pick_stream(info: dict, policy: str = "h264_mp4") -> dict | None:
    """
    Returns either:
      - {"kind":"muxed","url":..., "container":..., "codecs": "v+a"}
      - {"kind":"split","container":"mp4","video_url":..., "audio_url":...}
    Fallback order:
      1) muxed MP4 when policy == "h264_mp4"
      2) any muxed
      3) best video-only + best audio-only (split)
    """
    fmts = info.get("formats") or []

    best = None
    if policy == "h264_mp4":
        best = _best_muxed(fmts, ext_preference="mp4")
    if not best:
        best = _best_muxed(fmts)

    if best:
        container = best.get("container") or best.get("ext") or "mp4"
        v = (best.get("vcodec") or "").strip()
        a = (best.get("acodec") or "").strip()
        codecs = (v + "+" + a).strip("+") if (v or a) else ""
        return {"kind": "muxed", "url": best["url"], "container": container, "codecs": codecs}

    # Fallback to split remux
    vbest = _best_video(fmts)
    abest = best_audio(fmts)
    if vbest and abest:
        return {"kind": "split", "container": "mp4", "video_url": vbest["url"], "audio_url": abest["url"]}

    return None


def pick_by_itag(info: dict, itag: str | None) -> dict | None:
    """
    Honor a specific itag if provided. If the itag is video-only or audio-only,
    pair it with the best counterpart so playback can proceed.
    """
    if not itag:
        return None

    fmts = info.get("formats") or []
    target = next(
        (f for f in fmts if str(f.get("format_id") or f.get("itag")) == str(itag) and f.get("url")),
        None
    )
    if not target:
        return None

    if fmt_is_muxed(target):
        container = target.get("container") or target.get("ext") or "mp4"
        v = (target.get("vcodec") or "").strip()
        a = (target.get("acodec") or "").strip()
        codecs = (v + "+" + a).strip("+") if (v or a) else ""
        return {"kind": "muxed", "url": target["url"], "container": container, "codecs": codecs}

    if fmt_is_video_only(target):
        abest = best_audio(fmts)
        if abest:
            return {"kind": "split", "container": "mp4", "video_url": target["url"], "audio_url": abest["url"]}
        return None

    if fmt_is_audio_only(target):
        vbest = _best_video(fmts)
        if vbest:
            return {"kind": "split", "container": "mp4", "video_url": vbest["url"], "audio_url": target["url"]}
        return None

    return None
