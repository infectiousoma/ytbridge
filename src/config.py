import os, pathlib

BACKEND_PROVIDER = os.environ.get("BACKEND_PROVIDER", "invidious").strip().lower()
BACKEND_BASE     = os.environ.get("BACKEND_BASE", "https://yewtu.be").rstrip("/")
SPONSORBLOCK     = os.environ.get("SPONSORBLOCK", "true").strip().lower()

YTDLP_MODE       = os.environ.get("YTDLP_MODE", "local").strip().lower()  # "local" | "remote"
YTDLP_CMD        = os.environ.get("YTDLP_CMD", "yt-dlp").strip()
YTDLP_REMOTE_URL = os.environ.get("YTDLP_REMOTE_URL", "").strip()
FFMPEG_CMD       = os.environ.get("FFMPEG_CMD", "ffmpeg").strip()

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_TTL = int(os.environ.get("REDIS_TTL", "43200"))  # 12h

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PRIV = PROJECT_ROOT / "priv"
DEFAULT_DATA = DEFAULT_PRIV / "data"

DATA_DIR = os.environ.get("DATA_DIR", str(DEFAULT_DATA))
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

_env_cookie = os.environ.get("YTDLP_COOKIES", "").strip()
_default_cookie = DEFAULT_PRIV / "cookies.txt"
if not _env_cookie and _default_cookie.exists():
    COOKIES = str(_default_cookie)
else:
    COOKIES = _env_cookie

PORT = int(os.environ.get("PORT", "8080"))

SUBS_PATH = str(pathlib.Path(DATA_DIR) / "subscriptions.json")
FAVS_PATH = str(pathlib.Path(DATA_DIR) / "favorites.json")
