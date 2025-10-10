# config.py
import os, pathlib

# --- Upstream / discovery ---
BACKEND_PROVIDER = os.environ.get("BACKEND_PROVIDER", "invidious").strip().lower()
BACKEND_BASE     = os.environ.get("BACKEND_BASE", "https://yewtu.be").rstrip("/")
SPONSORBLOCK     = os.environ.get("SPONSORBLOCK", "true").strip().lower()

# --- yt-dlp / ffmpeg wiring ---
# Prefer YTDLP_BIN if present (compat with setups that export that), else YTDLP_CMD, else 'yt-dlp'
YTDLP_CMD        = os.environ.get("YTDLP_BIN", os.environ.get("YTDLP_CMD", "yt-dlp")).strip()
YTDLP_MODE       = os.environ.get("YTDLP_MODE", "local").strip().lower()  # "local" | "remote"
YTDLP_REMOTE_URL = os.environ.get("YTDLP_REMOTE_URL", "").strip()

# Extra yt-dlp args; defaults are chosen to keep stdout clean JSON for -J
YTDLP_ARGS = os.environ.get(
    "YTDLP_ARGS",
    "--ignore-config --no-warnings --no-progress --no-call-home"
).strip()

# ffmpeg path
FFMPEG_CMD       = os.environ.get("FFMPEG_CMD", "ffmpeg").strip()

# --- caching / storage ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_TTL = int(os.environ.get("REDIS_TTL", "43200"))  # 12h

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PRIV = PROJECT_ROOT / "priv"
DEFAULT_DATA = DEFAULT_PRIV / "data"

DATA_DIR = os.environ.get("DATA_DIR", str(DEFAULT_DATA))
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# Cookies: env wins; otherwise use priv/cookies.txt if it exists
_env_cookie = os.environ.get("YTDLP_COOKIES", "").strip()
_default_cookie = DEFAULT_PRIV / "cookies.txt"
if not _env_cookie and _default_cookie.exists():
    COOKIES = str(_default_cookie)
else:
    COOKIES = _env_cookie

# --- service port ---
PORT = int(os.environ.get("PORT", "8080"))

# --- data files ---
SUBS_PATH = str(pathlib.Path(DATA_DIR) / "subscriptions.json")
FAVS_PATH = str(pathlib.Path(DATA_DIR) / "favorites.json")
