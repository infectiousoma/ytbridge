# src/ytdlp_adapter.py
import json, os, re, shlex, subprocess
from fastapi import HTTPException
from . import config
from .cache import cache_get, cache_set
import httpx

# Extract the first JSON object/array from stdout even if warnings leak around it
_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)

def _env_flag(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if (v is not None and v != "") else default

def _build_local_cmd(url: str, net_pref: str | None, extra_args_env: str | None) -> list[str]:
    """
    Build a yt-dlp command that is quiet on stdout and safe for JSON parsing.
    - No deprecated --no-call-home.
    - net_pref: 'ipv4' | 'ipv6' | None
    - extra_args_env: string from YTDLP_ARGS (split with shlex)
    """
    safe = ["-J", "--ignore-config", "--no-warnings", "--no-progress"]
    cmd = [config.YTDLP_CMD] + safe

    # Network preference
    if net_pref == "ipv4":
        cmd += ["--force-ipv4"]
    elif net_pref == "ipv6":
        cmd += ["--force-ipv6"]

    # Optional extra args from env (e.g., throttling, proxies)
    if extra_args_env:
        cmd += shlex.split(extra_args_env)

    # Cookies / SponsorBlock
    if config.COOKIES:
        cmd += ["--cookies", config.COOKIES]
    if str(config.SPONSORBLOCK).lower() == "true":
        cmd += ["--sponsorblock-mark", "all"]

    cmd += [url]
    return cmd

def _looks_like_net_fail(stderr: str) -> bool:
    s = (stderr or "").lower()
    # match common failure fragments
    needles = [
        "timed out", "temporarily unavailable", "temporary failure",
        "connection refused", "network is unreachable",
        "cannot assign requested address", "failed to resolve",
        "tlsv1 alert", "proxy error", "transporterror"
    ]
    return any(n in s for n in needles)

def _parse_json_or_bust(stdout: str, returncode: int, stderr_tail: str) -> dict:
    # 1) try clean parse
    try:
        obj = json.loads(stdout)
        if obj is None:
            raise ValueError("yt-dlp returned JSON null")
        return obj
    except Exception:
        pass
    # 2) try to extract the object/array from noisy stdout
    m = _JSON_RE.search(stdout or "")
    if m:
        try:
            obj = json.loads(m.group(1))
            if obj is None:
                raise ValueError("yt-dlp returned JSON null")
            return obj
        except Exception:
            pass
    # 3) fail with a terse message (avoid dumping full stderr)
    raise HTTPException(502, f"Failed to parse yt-dlp JSON (rc={returncode}). {stderr_tail}")

def _run_local(url: str, net_pref: str | None, extra_args_env: str | None) -> dict:
    cmd = _build_local_cmd(url, net_pref, extra_args_env)
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise HTTPException(500, f"yt-dlp not found at '{config.YTDLP_CMD}'. Set YTDLP_CMD or mount the binary.")

    stderr_tail = (p.stderr or "").strip()
    if len(stderr_tail) > 220:
        stderr_tail = stderr_tail[-220:]

    # If stdout is empty or literally "null", surface a clean error
    if not (p.stdout and p.stdout.strip()) or p.stdout.strip() == "null":
        hint = "network error" if _looks_like_net_fail(p.stderr) else "no output"
        raise HTTPException(502, f"yt-dlp returned no data ({hint}). {stderr_tail}")

    return _parse_json_or_bust(p.stdout or "", p.returncode, stderr_tail)

def _local_ytdlp_dump(url: str) -> dict:
    """
    IPv4-first by default. Behavior controlled by:
      - YTDLP_NET: 'ipv4' (default), 'ipv6', or 'auto'
      - YTDLP_ARGS: extra args (e.g. proxies). If you explicitly pass
        --force-ipv4/--force-ipv6 here, we won't override it.
    """
    net_mode = (_env_flag("YTDLP_NET", "ipv4") or "ipv4").lower()  # ipv4 | ipv6 | auto
    extra_env = _env_flag("YTDLP_ARGS", None)

    # If caller already forces an IP version in YTDLP_ARGS, just run once
    pre_forced_v4 = extra_env and "--force-ipv4" in extra_env
    pre_forced_v6 = extra_env and "--force-ipv6" in extra_env
    if pre_forced_v4:
        return _run_local(url, None, extra_env)  # respect explicit args
    if pre_forced_v6:
        return _run_local(url, None, extra_env)

    # Normal flow: prefer ipv4, or ipv6, or auto (try v4 then v6)
    if net_mode == "ipv6":
        # try v6, fallback to v4 on obvious network errors
        try:
            return _run_local(url, "ipv6", extra_env)
        except HTTPException as e:
            if getattr(e, "status_code", 502) >= 500 and "network" in str(getattr(e, "detail", "")).lower():
                return _run_local(url, "ipv4", extra_env)
            raise
    elif net_mode == "auto":
        # try v4, fallback to v6 on obvious network errors
        try:
            return _run_local(url, "ipv4", extra_env)
        except HTTPException as e:
            if getattr(e, "status_code", 502) >= 500 and "network" in str(getattr(e, "detail", "")).lower():
                return _run_local(url, "ipv6", extra_env)
            raise
    else:
        # default: ipv4-first with no fallback (to avoid v6 surprises on hosts without v6)
        return _run_local(url, "ipv4", extra_env)

def _remote_ytdlp_dump(url: str) -> dict:
    if not config.YTDLP_REMOTE_URL:
        raise HTTPException(500, "YTDLP_REMOTE_URL not set for remote mode")
    q = {"url": url}
    if config.COOKIES:
        q["cookies"] = config.COOKIES
    if str(config.SPONSORBLOCK).lower() == "true":
        q["sponsorblock"] = "all"
    try:
        with httpx.Client(timeout=60) as cx:
            r = cx.get(config.YTDLP_REMOTE_URL, params=q)
    except Exception as e:
        raise HTTPException(502, f"yt-dlp remote error: {e}")
    if r.status_code != 200:
        txt = (r.text or "")[:200]
        raise HTTPException(r.status_code, f"yt-dlp remote status {r.status_code}: {txt}")
    try:
        obj = r.json()
    except Exception:
        raise HTTPException(502, "yt-dlp remote returned non-JSON")
    if obj is None:
        raise HTTPException(502, "yt-dlp remote returned no data")
    return obj

def ytdlp_dump(video_id: str) -> dict:
    ck = f"ytdlp:video:{video_id}"
    cached = cache_get(ck)
    if cached:
        try:
            obj = json.loads(cached)
            if isinstance(obj, dict) and obj:
                return obj
        except Exception:
            pass
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _remote_ytdlp_dump(url) if config.YTDLP_MODE == "remote" else _local_ytdlp_dump(url)
    # cache best-effort
    try:
        cache_set(ck, json.dumps(info))
    except Exception:
        pass
    return info
