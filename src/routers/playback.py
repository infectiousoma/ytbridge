# routers/playback.py
import asyncio
import os
import re
import subprocess
from typing import Optional, Tuple
from contextlib import AsyncExitStack

from fastapi import APIRouter, Request, HTTPException, Response, Query
from fastapi.responses import StreamingResponse, RedirectResponse
import httpx

from .. import config
from ..ytdlp_adapter import ytdlp_dump
from ..format_utils import yt_headers, merge_headers
from ..select_utils import pick_stream, pick_by_itag
from ..http_utils import headers_kv

router = APIRouter()


# -----------------------------
# Helpers / predicates
# -----------------------------
def _good_muxed(s: dict | None) -> bool:
    return bool(s and s.get("kind") == "muxed" and s.get("url"))


def _is_hls_url(u: str | None) -> bool:
    if not u:
        return False
    # Most YouTube HLS manifests look like .../manifest/hls_playlist/... or end with .m3u8
    return "manifest/hls_playlist" in u or u.endswith(".m3u8")


def _is_hls_stream(s: dict | None) -> bool:
    return bool(s and s.get("url") and _is_hls_url(s["url"]))


def _want_redirect(force_redirect: Optional[bool]) -> bool:
    """
    Decide redirect policy:
      - True  -> redirect
      - False -> proxy
      - None  -> follow global STREAM_MODE
    """
    if force_redirect is not None:
        return bool(force_redirect)
    return getattr(config, "STREAM_MODE", "proxy").lower() == "redirect"


async def _open_upstream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    refresh_cb,
    allow_refresh: bool = True,
) -> Tuple[httpx.Response, AsyncExitStack]:
    """
    Open an upstream GET as a streaming response (without buffering the body).
    If we see 403/410 once, call refresh_cb() to get a new (url, headers) and retry once.

    Returns (response, exit_stack). The caller must close both (by calling
    resp.aclose() and stack.aclose()) OR just stack.aclose() which closes the CM.
    """
    stack = AsyncExitStack()
    resp = await stack.enter_async_context(client.stream("GET", url, headers=headers))

    if resp.status_code in (403, 410) and allow_refresh:
        await stack.aclose()
        new_url, new_headers = await refresh_cb()
        stack = AsyncExitStack()
        resp = await stack.enter_async_context(
            client.stream("GET", new_url, headers=new_headers)
        )

    return resp, stack


def _copy_resp_headers_from_upstream(up: httpx.Response, default_ct: str = "video/mp4") -> dict:
    out = {}
    for h in [
        "Content-Type",
        "Content-Length",      # only if upstream provided
        "Accept-Ranges",
        "Content-Range",
        "Last-Modified",
        "ETag",
        "Cache-Control",
    ]:
        v = up.headers.get(h)
        if v:
            out[h] = v
    # Reasonable fallbacks
    out.setdefault("Accept-Ranges", "bytes")
    out.setdefault("Content-Type", default_ct)
    out.setdefault("Cache-Control", "no-store")
    return out


def _find_any_hls(info: dict) -> Optional[dict]:
    """
    From a ytdlp_dump(info), find the first HLS-like format (preferring 94/95/96).
    """
    # Prefer 94, then 95, then 96 if present
    for pref in ("94", "95", "96"):
        cand = pick_by_itag(info, pref)
        if _is_hls_stream(cand):
            return cand
    # Otherwise, scan formats for an hls-like url
    for f in info.get("formats", []):
        if _is_hls_url(f.get("url")):
            # provide a minimal shape compatible with our handlers
            return {
                "kind": "hls",
                "itag": str(f.get("itag")) if f.get("itag") is not None else None,
                "url": f.get("url"),
            }
    return None


# -----------------------------
# HLS endpoint (Option B)
# -----------------------------
@router.get("/hls/{video_id}")
async def hls(
    video_id: str,
    itag: Optional[str] = Query(default="94", description="HLS itag: 94/95/96; if missing/invalid, first HLS is used"),
    force_redirect: Optional[bool] = None,
    debug: int = 0,
):
    """
    Return an HLS manifest for the requested video.
    - redirect mode: 302 to the googlevideo manifest
    - proxy mode: fetch the m3u8 and return it with m3u8 content-type
    """
    info = ytdlp_dump(video_id)
    s = pick_by_itag(info, itag) if itag else None
    if not _is_hls_stream(s):
        s = _find_any_hls(info)

    if not s or not s.get("url"):
        raise HTTPException(404, "No HLS manifest available for this video")

    want_redirect = _want_redirect(force_redirect)

    if want_redirect:
        # Simple 302 to the manifest
        resp = RedirectResponse(s["url"], status_code=302)
        if debug:
            resp.headers["x-ytbridge-mode"] = "redirect"
            resp.headers["x-ytbridge-kind"] = "hls"
            resp.headers["x-ytbridge-itag"] = str(s.get("itag"))
        return resp

    # Proxy mode: fetch m3u8 content and return as text
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx:
        r = await cx.get(s["url"], headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code >= 400:
        raise HTTPException(502, f"Failed to fetch HLS manifest (HTTP {r.status_code})")

    headers = {
        "Content-Type": "application/vnd.apple.mpegurl",
        "Cache-Control": "private, max-age=30",
    }
    if debug:
        headers["x-ytbridge-mode"] = "proxy"
        headers["x-ytbridge-kind"] = "hls"
        headers["x-ytbridge-itag"] = str(s.get("itag"))
    return Response(content=r.text, headers=headers, status_code=200)


# -----------------------------
# /play – extended with HLS fallback
# -----------------------------
@router.get("/play/{video_id}")
async def play(
    video_id: str,
    request: Request,
    policy: str = "h264_mp4",
    itag: str | None = None,
    force_redirect: Optional[bool] = None,
    debug: int = 0,
):
    info = ytdlp_dump(video_id)
    stream = pick_by_itag(info, itag) if itag else pick_stream(info, policy)
    if not stream:
        # immediate attempt to serve HLS if available
        hls = _find_any_hls(info)
        if hls:
            # Defer to /hls handler logic (don’t duplicate)
            want_redirect = _want_redirect(force_redirect)
            if want_redirect:
                return RedirectResponse(hls["url"], status_code=302)
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx:
                r = await cx.get(hls["url"], headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400:
                raise HTTPException(502, f"Failed to fetch HLS manifest (HTTP {r.status_code})")
            return Response(
                content=r.text,
                status_code=200,
                headers={"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "private, max-age=30"},
            )
        raise HTTPException(502, "No playable stream (progressive or split) found")

    want_redirect = _want_redirect(force_redirect)

    # --- If the chosen stream is already HLS, serve it now ---
    if _is_hls_stream(stream):
        if want_redirect:
            resp = RedirectResponse(stream["url"], status_code=302)
            if debug:
                resp.headers["x-ytbridge-mode"] = "redirect"
                resp.headers["x-ytbridge-kind"] = "hls"
                resp.headers["x-ytbridge-itag"] = str(stream.get("itag"))
            return resp
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx:
            r = await cx.get(stream["url"], headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            raise HTTPException(502, f"HLS manifest fetch failed (HTTP {r.status_code})")
        headers = {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "private, max-age=30",
        }
        if debug:
            headers["x-ytbridge-mode"] = "proxy"
            headers["x-ytbridge-kind"] = "hls"
            headers["x-ytbridge-itag"] = str(stream.get("itag"))
        return Response(content=r.text, headers=headers, status_code=200)

    # --- Redirect path (only for progressive/muxed) ---
    if want_redirect and _good_muxed(stream):
        return RedirectResponse(stream["url"], status_code=302)

    # --- Proxy path for muxed (progressive) streams ---
    if _good_muxed(stream):
        target = stream["url"]

        # Build upstream headers. If client didn't send Range, force 0- to make upstream return 206+Content-Range.
        passthru = {}
        client_range = request.headers.get("Range")
        if client_range:
            passthru["Range"] = client_range
        else:
            passthru["Range"] = "bytes=0-"
        if request.headers.get("If-Range"):
            passthru["If-Range"] = request.headers["If-Range"]
        base_hdrs = merge_headers(yt_headers(info), passthru)

        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as cx:

            async def refresh_once():
                _info2 = ytdlp_dump(video_id)
                _s2 = pick_by_itag(_info2, itag) if itag else pick_stream(_info2, policy)
                if not _good_muxed(_s2):
                    # Try HLS fallback on refresh failure
                    _h = _find_any_hls(_info2)
                    if _h:
                        if want_redirect:
                            return RedirectResponse(_h["url"], status_code=302)
                        # proxy the m3u8
                        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx2:
                            r = await cx2.get(_h["url"], headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code >= 400:
                            raise HTTPException(502, f"HLS manifest fetch failed (HTTP {r.status_code})")
                        return None, None  # signal caller to handle HLS immediately
                    raise HTTPException(502, "Upstream URL expired and refresh failed")
                _hdrs2 = merge_headers(yt_headers(_info2), {"Range": passthru["Range"]})
                if request.headers.get("If-Range"):
                    _hdrs2["If-Range"] = request.headers["If-Range"]
                return _s2["url"], _hdrs2

            # Open the actual upstream stream now so we can mirror *real* status+headers.
            up, stack = await _open_upstream(cx, target, base_hdrs, refresh_once, allow_refresh=True)

            # If refresh_once signaled HLS handling (None, None), serve HLS now
            if up is None:
                await stack.aclose()
                _hls = _find_any_hls(info) or _find_any_hls(ytdlp_dump(video_id))
                if not _hls:
                    raise HTTPException(502, "HLS fallback not available")
                if want_redirect:
                    return RedirectResponse(_hls["url"], status_code=302)
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx3:
                    r = await cx3.get(_hls["url"], headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code >= 400:
                    raise HTTPException(502, f"HLS manifest fetch failed (HTTP {r.status_code})")
                return Response(
                    content=r.text,
                    status_code=200,
                    headers={"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "private, max-age=30"},
                )

            # If mp4 returns non-OK, try HLS immediately
            if up.status_code not in (200, 206):
                await stack.aclose()
                hls = _find_any_hls(info) or _find_any_hls(ytdlp_dump(video_id))
                if hls:
                    if want_redirect:
                        return RedirectResponse(hls["url"], status_code=302)
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx2:
                        r = await cx2.get(hls["url"], headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code >= 400:
                        raise HTTPException(502, f"HLS manifest fetch failed (HTTP {r.status_code})")
                    return Response(
                        content=r.text,
                        status_code=200,
                        headers={"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "private, max-age=30"},
                    )
                detail = f"Upstream responded {up.status_code}"
                raise HTTPException(up.status_code, detail)

            resp_headers = _copy_resp_headers_from_upstream(up, default_ct="video/mp4")

            # Optional debug headers
            if debug:
                resp_headers["x-ytbridge-mode"] = "proxy"
                resp_headers["x-ytbridge-want-redirect"] = str(bool(want_redirect))
                resp_headers["x-ytbridge-policy"] = policy
                resp_headers["x-ytbridge-itag"] = str(itag)
                resp_headers["x-ytbridge-kind"] = "muxed"

            async def bodygen():
                try:
                    async for chunk in up.aiter_bytes():
                        yield chunk
                finally:
                    try:
                        await stack.aclose()
                    except Exception:
                        pass

            # Mirror actual upstream status (200 or 206)
            return StreamingResponse(
                bodygen(),
                status_code=up.status_code,
                headers=resp_headers,
                media_type=resp_headers.get("Content-Type", "video/mp4"),
            )

    # --- Split (video+audio) → live remux (no ranges) ---
    if stream.get("kind") == "split" and stream.get("video_url") and stream.get("audio_url"):
        v = stream["video_url"]
        a = stream["audio_url"]
        yt_hdrs = yt_headers(info)
        cmd = [
            config.FFMPEG_CMD,
            "-loglevel",
            "error",
            "-nostdin",
            "-hide_banner",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            "15000000",
            *headers_kv(yt_hdrs),
            "-i",
            v,
            *headers_kv(yt_hdrs),
            "-i",
            a,
            "-c",
            "copy",
            "-movflags",
            "+frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "pipe:1",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError:
            raise HTTPException(500, f"ffmpeg not found at '{config.FFMPEG_CMD}'.")

        async def gen():
            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass

        headers = {"Accept-Ranges": "none", "Cache-Control": "no-store"}
        if debug:
            headers["x-ytbridge-mode"] = "remux"
            headers["x-ytbridge-want-redirect"] = str(bool(want_redirect))
            headers["x-ytbridge-policy"] = policy
            headers["x-ytbridge-itag"] = str(itag)
            headers["x-ytbridge-kind"] = "split"
        return StreamingResponse(gen(), media_type="video/mp4", headers=headers)

    # If we got here and nothing was playable, attempt HLS one last time
    hls = _find_any_hls(info)
    if hls:
        if _want_redirect(force_redirect):
            return RedirectResponse(hls["url"], status_code=302)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cx:
            r = await cx.get(hls["url"], headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            raise HTTPException(502, f"HLS manifest fetch failed (HTTP {r.status_code})")
        return Response(
            content=r.text,
            status_code=200,
            headers={"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "private, max-age=30"},
        )

    raise HTTPException(502, "No playable stream (progressive or split) found")


@router.head("/play/{video_id}")
async def play_head(
    video_id: str,
    request: Request,
    policy: str = "h264_mp4",
    itag: str | None = None,
    force_redirect: Optional[bool] = None,
    debug: int = 0,
):
    info = ytdlp_dump(video_id)
    stream = pick_by_itag(info, itag) if itag else pick_stream(info, policy)
    want_redirect = _want_redirect(force_redirect)

    # If the chosen stream is HLS, just acknowledge with m3u8 headers
    if _is_hls_stream(stream):
        headers = {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Accept-Ranges": "none",
            "Cache-Control": "no-store",
        }
        if debug:
            headers["x-ytbridge-mode"] = "head-hls"
            headers["x-ytbridge-want-redirect"] = str(bool(want_redirect))
            headers["x-ytbridge-policy"] = policy
            headers["x-ytbridge-itag"] = str(itag)
            headers["x-ytbridge-kind"] = "hls"
        return Response(status_code=200, headers=headers)

    # Redirect mode for muxed: issue 302
    if want_redirect and _good_muxed(stream):
        return RedirectResponse(stream["url"], status_code=302)

    # Proxy HEAD for muxed: do a tiny GET with Range: bytes=0-0 to fetch *real* headers.
    if _good_muxed(stream):
        target = stream["url"]
        passthru_range = request.headers.get("Range") or "bytes=0-0"
        hdrs = merge_headers(yt_headers(info), {"Range": passthru_range})
        if request.headers.get("If-Range"):
            hdrs["If-Range"] = request.headers["If-Range"]

        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as cx:

            async def refresh_once():
                _info2 = ytdlp_dump(video_id)
                _s2 = pick_by_itag(_info2, itag) if itag else pick_stream(_info2, policy)
                if not _good_muxed(_s2):
                    # Say OK but with generic headers; Jellyfin will attempt GET and fallback to HLS path in GET handler
                    return target, hdrs
                _hdrs2 = merge_headers(yt_headers(_info2), {"Range": passthru_range})
                if request.headers.get("If-Range"):
                    _hdrs2["If-Range"] = request.headers["If-Range"]
                return _s2["url"], _hdrs2

            up, stack = await _open_upstream(cx, target, hdrs, refresh_once, allow_refresh=True)

            status = up.status_code if up.status_code in (200, 206) else 200
            resp_headers = _copy_resp_headers_from_upstream(up, default_ct="video/mp4")
            if debug:
                resp_headers["x-ytbridge-mode"] = "proxy-head"
                resp_headers["x-ytbridge-want-redirect"] = str(bool(want_redirect))
                resp_headers["x-ytbridge-policy"] = policy
                resp_headers["x-ytbridge-itag"] = str(itag)
                resp_headers["x-ytbridge-kind"] = "muxed"

            await stack.aclose()
        return Response(status_code=status, headers=resp_headers)

    # Non-muxed generic HEAD OK
    headers = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if debug:
        headers["x-ytbridge-mode"] = "head-generic"
        headers["x-ytbridge-want-redirect"] = str(bool(want_redirect))
        headers["x-ytbridge-policy"] = policy
        headers["x-ytbridge-itag"] = str(itag)
        headers["x-ytbridge-kind"] = "other"
    return Response(status_code=200, headers=headers)
