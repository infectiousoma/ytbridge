# routers/playback.py
import subprocess
from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, RedirectResponse
import httpx

from .. import config
from ..ytdlp_adapter import ytdlp_dump
from ..format_utils import yt_headers, merge_headers
from ..select_utils import pick_stream, pick_by_itag
from ..http_utils import probe_headers, headers_kv

router = APIRouter()

@router.get("/play/{video_id}")
async def play(video_id: str, request: Request, policy: str = "h264_mp4", itag: str | None = None):
    info = ytdlp_dump(video_id)
    stream = pick_by_itag(info, itag) if itag else pick_stream(info, policy)
    if not stream:
        raise HTTPException(502, "No playable stream (progressive or split) found")

    # Progressive (muxed): proxy with ranges + refresh once on 403/410
    if stream.get("kind") == "muxed" and "url" in stream:
        target = stream["url"]
        passthru = {}
        if request.headers.get("Range"):    passthru["Range"]    = request.headers["Range"]
        if request.headers.get("If-Range"): passthru["If-Range"] = request.headers["If-Range"]
        hdrs = merge_headers(yt_headers(info), passthru)
        if getattr(config, "STREAM_MODE", "proxy") == "redirect":
           return RedirectResponse(target, status_code=302)

        async def generator(target_url: str, hdrs: dict):
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as cx:
                attempt = 0
                current_url = target_url
                current_hdrs = hdrs
                while True:
                    try:
                        async with cx.stream("GET", current_url, headers=current_hdrs) as resp:
                            # Refresh once if the signed URL is expired/forbidden
                            if resp.status_code in (403, 410):
                                if attempt < 1:
                                    attempt += 1
                                    try:
                                        info3 = ytdlp_dump(video_id)
                                        stream3 = pick_by_itag(info3, itag) if itag else pick_stream(info3, policy)
                                        if not (stream3 and stream3.get("kind") == "muxed" and "url" in stream3):
                                            return
                                        current_url  = stream3["url"]
                                        current_hdrs = merge_headers(yt_headers(info3), passthru)
                                        continue
                                    except Exception:
                                        return
                                return
                            if resp.status_code not in (200, 206):
                                return
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                            break
                    except Exception:
                        # Treat network hiccups as end-of-stream; Jellyfin will retry
                        return

        # Preflight HEAD for response headers; refresh once if needed
        hr = None
        try:
            hr = await probe_headers(target, hdrs)
        except Exception:
            hr = None

        # If the signed URL is stale, refresh once here as well (mirrors GET path)
        if hr is not None and hr.status_code in (403, 410):
            try:
                info2 = ytdlp_dump(video_id)
                stream2 = pick_by_itag(info2, itag) if itag else pick_stream(info2, policy)
                if stream2 and stream2.get("kind") == "muxed" and "url" in stream2:
                    target = stream2["url"]
                    hdrs = merge_headers(yt_headers(info2), passthru)
                    try:
                        hr = await probe_headers(target, hdrs)
                    except Exception:
                        hr = None
            except Exception:
                pass

        # If HEAD wasn't helpful (many YT edges don’t include Content-Range on HEAD),
        # do a tiny ranged GET to infer headers when the client asked for Range.
        if hdrs.get("Range") and (hr is None or ("Content-Range" not in (hr.headers or {}))):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cx:
                    async with cx.stream("GET", target, headers=hdrs) as gr:
                        # Drop body; we only want headers/status
                        await gr.aclose()
                        class _H: ...
                        _H.status_code = gr.status_code
                        _H.headers = gr.headers
                        hr = _H
            except Exception:
                # Don’t block; we’ll let the streaming generator handle it
                pass

        resp_headers, status = {}, 200
        if hr is not None:
            status = 206 if hdrs.get("Range") and hr.status_code in (200, 206) else 200
            for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Last-Modified", "ETag"]:
                if h in hr.headers:
                    resp_headers[h] = hr.headers[h]
        resp_headers.setdefault("Accept-Ranges", "bytes")
        return StreamingResponse(generator(target, hdrs), status_code=status, headers=resp_headers)

    # Split (video-only + audio-only) → live remux (no ranges)
    if stream.get("kind") == "split" and stream.get("video_url") and stream.get("audio_url"):
        v = stream["video_url"]; a = stream["audio_url"]
        yt_hdrs = yt_headers(info)
        cmd = [
            config.FFMPEG_CMD, "-loglevel", "error", "-nostdin", "-hide_banner",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
            *headers_kv(yt_hdrs), "-i", v,
            *headers_kv(yt_hdrs), "-i", a,
            "-c", "copy",
            "-movflags", "+frag_keyframe+empty_moov",
            "-f", "mp4", "pipe:1",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except FileNotFoundError:
            raise HTTPException(500, f"ffmpeg not found at '{config.FFMPEG_CMD}'. Set FFMPEG_CMD or install ffmpeg.")

        async def gen():
            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    rc = proc.poll()
                    if rc not in (0, None):
                        try:
                            _ = (proc.stderr.read() or b"").decode("utf-8", "ignore")
                        except Exception:
                            pass
                finally:
                    try: proc.kill()
                    except: pass

        return StreamingResponse(gen(), media_type="video/mp4", headers={"Accept-Ranges": "none"})

    raise HTTPException(502, "No playable stream (progressive or split) found")

@router.head("/play/{video_id}")
async def play_head(video_id: str, request: Request, policy: str = "h264_mp4", itag: str | None = None):
    info = ytdlp_dump(video_id)
    stream = pick_by_itag(info, itag) if itag else pick_stream(info, policy)

    if stream and stream.get("kind") == "muxed" and "url" in stream:
        target  = stream["url"]
        yt_hdrs = yt_headers(info)
        passthru = {}
        if request.headers.get("Range"):    passthru["Range"]    = request.headers["Range"]
        if request.headers.get("If-Range"): passthru["If-Range"] = request.headers["If-Range"]
        headers = merge_headers(yt_hdrs, passthru)

    if getattr(config, "STREAM_MODE", "proxy") == "redirect":
        return RedirectResponse(target, status_code=302)
        
        hr = None
        try:
            hr = await probe_headers(target, headers)
        except Exception:
            hr = None

        # refresh once if the signed URL is stale (403/410), mirroring GET path
        if hr is not None and hr.status_code in (403, 410):
            try:
                info2 = ytdlp_dump(video_id)
                stream2 = pick_by_itag(info2, itag) if itag else pick_stream(info2, policy)
                if stream2 and stream2.get("kind") == "muxed" and "url" in stream2:
                    target = stream2["url"]
                    headers = merge_headers(yt_headers(info2), passthru)
                    try:
                        hr = await probe_headers(target, headers)
                    except Exception:
                        hr = None
            except Exception:
                pass

        # Fallback to tiny ranged GET if HEAD lacks useful headers
        if headers.get("Range") and (hr is None or ("Content-Range" not in (hr.headers or {}))):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cx:
                    async with cx.stream("GET", target, headers=headers) as gr:
                        await gr.aclose()
                        class _H: ...
                        _H.status_code = gr.status_code
                        _H.headers = gr.headers
                        hr = _H
            except Exception:
                pass

        resp_headers, status = {}, 200
        if hr is not None:
            if headers.get("Range") and (hr.status_code in (200, 206)):
                status = 206 if ("Content-Range" in hr.headers) else 200
            for h in ["Content-Type", "Content-Length", "Accept-Ranges", "Content-Range", "Last-Modified", "ETag"]:
                if h in hr.headers:
                    resp_headers[h] = hr.headers[h]
        resp_headers.setdefault("Accept-Ranges", "bytes")
        return Response(status_code=status, headers=resp_headers)

    # Generic OK for clients that only preflight the endpoint
    return Response(status_code=200, headers={"Content-Type": "video/mp4"})
