from .format_utils import fmt_is_muxed, fmt_is_video_only, fmt_is_audio_only, best_audio

def pick_stream(info: dict, policy: str = "h264_mp4") -> dict | None:
    fmts = info.get("formats") or []
    best = None
    if policy == "h264_mp4":
        mp4s = [f for f in fmts if (f.get("container") == "mp4" or f.get("ext") == "mp4")
                and fmt_is_muxed(f) and f.get("url")]
        if mp4s:
            best = max(mp4s, key=lambda f: f.get("tbr") or 0)
    if not best:
        muxed = [f for f in fmts if fmt_is_muxed(f) and f.get("url")]
        if muxed:
            best = max(muxed, key=lambda f: f.get("tbr") or 0)
    if not best:
        return None
    container = best.get("container") or best.get("ext") or "mp4"
    v = best.get("vcodec") or ""
    a = best.get("acodec") or ""
    return {"kind": "muxed", "url": best["url"], "container": container, "codecs": f"{v}+{a}".strip("+")}

def pick_by_itag(info: dict, itag: str | None) -> dict | None:
    if not itag:
        return None
    fmts = info.get("formats") or []
    target = next((f for f in fmts
                   if str(f.get("format_id") or f.get("itag")) == str(itag) and f.get("url")), None)
    if not target:
        return None
    if fmt_is_muxed(target):
        container = target.get("container") or target.get("ext") or "mp4"
        v = target.get("vcodec") or ""
        a = target.get("acodec") or ""
        return {"kind": "muxed", "url": target["url"], "container": container, "codecs": f"{v}+{a}".strip("+")}
    if fmt_is_video_only(target):
        abest = best_audio(fmts)
        if abest:
            return {"kind": "split", "container": "mp4", "video_url": target["url"], "audio_url": abest["url"]}
        return None
    if fmt_is_audio_only(target):
        vids = [f for f in fmts if fmt_is_video_only(f) and f.get("url")]
        if vids:
            vbest = max(vids, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))
            return {"kind": "split", "container": "mp4", "video_url": vbest["url"], "audio_url": target["url"]}
        return None
    return None
