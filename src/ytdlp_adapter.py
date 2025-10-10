# src/ytdlp_adapter.py
import json, os, re, shlex, subprocess, httpx
from fastapi import HTTPException
from . import config
from .cache import cache_get, cache_set

# Extract the first JSON object/array from stdout even if warnings leak around it
_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)

def _build_local_cmd(url: str) -> list[str]:
    """
    Build a yt-dlp command that is quiet on stdout and safe for JSON parsing.
    We keep cookies/sponsorblock behavior you already had.
    """
    safe = ["-J", "--ignore-config", "--no-warnings", "--no-progress", "--no-call-home"]
    # Optional extra args from env (e.g., "--force-ipv4")
    env_extra = shlex.split(os.environ.get("YTDLP_ARGS", "")) if os.environ.get("YTDLP_ARGS") else []
    cmd = [config.YTDLP_CMD] + safe + env_extra + [url]
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

def _parse_json_or_bust(stdout: str, returncode: int, stderr_tail: str) -> dict:
    # 1) try clean parse
    try:
        return json.loads(stdout)
    except Exception:
        pass
    # 2) try to extract the object/array from noisy stdout
    m = _JSON_RE.search(stdout or "")
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3) fail with a terse message (avoid dumping full stderr)
    raise HTTPException(502, f"Failed to parse yt-dlp JSON (rc={returncode}). {stderr_tail}")

def _local_ytdlp_dump(url: str) -> dict:
    cmd = _build_local_cmd(url)
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,  # keep stdout clean
            stderr=subprocess.PIPE,  # keep stderr separate
            text=True,
            check=False,             # we'll still try to parse on non-zero rc
        )
    except FileNotFoundError:
        raise HTTPException(500, f"yt-dlp not found at '{config.YTDLP_CMD}'. Set YTDLP_CMD or mount the binary.")
    # Be conservative about what we surface
    stderr_tail = (p.stderr or "").strip()
    if len(stderr_tail) > 220:
        stderr_tail = stderr_tail[-220:]
    try:
        return _parse_json_or_bust(p.stdout or "", p.returncode, stderr_tail)
    except HTTPException as e:
        # Include a short hint if yt-dlp printed something recognizable
        detail = getattr(e, "detail", "yt-dlp failed")
        raise HTTPException(502, f"yt-dlp failed: {detail}")

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
