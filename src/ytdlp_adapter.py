import json, subprocess, httpx
from fastapi import HTTPException
from . import config
from .cache import cache_get, cache_set

def _build_local_cmd(url: str) -> list[str]:
    cmd = [config.YTDLP_CMD, url, "--dump-json", "--no-warnings"]
    if config.COOKIES:
        cmd += ["--cookies", config.COOKIES]
    if config.SPONSORBLOCK == "true":
        cmd += ["--sponsorblock-mark", "all"]
    return cmd

def _remote_ytdlp_dump(url: str) -> dict:
    if not config.YTDLP_REMOTE_URL:
        raise HTTPException(500, "YTDLP_REMOTE_URL not set for remote mode")
    q = {"url": url}
    if config.COOKIES:
        q["cookies"] = config.COOKIES
    if config.SPONSORBLOCK == "true":
        q["sponsorblock"] = "all"
    try:
        with httpx.Client(timeout=60) as cx:
            r = cx.get(config.YTDLP_REMOTE_URL, params=q)
    except Exception as e:
        raise HTTPException(502, f"yt-dlp remote error: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"yt-dlp remote status {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(502, "yt-dlp remote returned non-JSON")

def _local_ytdlp_dump(url: str) -> dict:
    cmd = _build_local_cmd(url)
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise HTTPException(500, f"yt-dlp not found at '{config.YTDLP_CMD}'. Set YTDLP_CMD or mount the binary.")
    except subprocess.CalledProcessError as e:
        tail = (e.output or "")[-400:]
        raise HTTPException(502, f"yt-dlp failed: {tail}")
    try:
        return json.loads(out)
    except Exception:
        raise HTTPException(502, "Failed to parse yt-dlp JSON")

def ytdlp_dump(video_id: str) -> dict:
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _remote_ytdlp_dump(url) if config.YTDLP_MODE == "remote" else _local_ytdlp_dump(url)
    try:
        cache_set(ck, json.dumps(info))
    except Exception:
        pass
    return info
