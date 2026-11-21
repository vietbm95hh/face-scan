"""Cấu hình chung: đọc .env, cung cấp đường dẫn và hằng số."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# `.env` là nguồn cấu hình duy nhất của tool. Dùng override=True để biến môi
# trường còn sót trong terminal (vd PG_HOST của server cũ) không ghi đè nhầm.
load_dotenv(ROOT / ".env", override=True)


def _env(key: str, default: str = "") -> str:
    """Đọc biến môi trường; bỏ dấu nháy kép/đơn nếu .env có bọc (vd PG_PASSWORD="abc@123")."""
    raw = os.getenv(key, default).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    return raw


def _int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# --- Database -------------------------------------------------------------
PG_HOST = _env("PG_HOST", "127.0.0.1")
PG_PORT = _int("PG_PORT", 2024)
PG_DB = _env("PG_DB", "fbscan")
PG_USER = _env("PG_USER", "postgres")
PG_PASSWORD = _env("PG_PASSWORD", "")
# Server dev ở xa: nếu mạng rớt, fail sau 10s thay vì treo vô hạn.
PG_CONNECT_TIMEOUT = _int("PG_CONNECT_TIMEOUT", 10)
# Tailscale/DERP có thể chập chờn vài giây: thử lại kết nối trước khi báo lỗi.
PG_CONNECT_RETRIES = max(1, _int("PG_CONNECT_RETRIES", 2))
PG_CONNECT_BACKOFF = float(_env("PG_CONNECT_BACKOFF", "1.0"))

DATABASE_URL = (
    f"postgresql://{quote(PG_USER)}:{quote(PG_PASSWORD)}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    if PG_PASSWORD
    else f"postgresql://{quote(PG_USER)}@{PG_HOST}:{PG_PORT}/{PG_DB}"
)

# --- Facebook -------------------------------------------------------------
COOKIES_FILE = ROOT / _env("FB_COOKIES_FILE", "facebook_cookies.json")
STATE_FILE = ROOT / "facebook_state.json"

# Số request mỗi phút tối đa khi quét (0 = không giới hạn). Tránh FB checkpoint.
REQUEST_DELAY_SEC = float(_env("REQUEST_DELAY_SEC", "1.0"))
GROUP_DELAY_MIN = float(_env("GROUP_DELAY_MIN", "5"))
GROUP_DELAY_MAX = float(_env("GROUP_DELAY_MAX", "15"))

# Dừng sớm khi gặp N bài liên tiếp đã có trong DB và cũ hơn MAX_AGE_DAYS.
MAX_AGE_DAYS = _int("MAX_AGE_DAYS", 7)
STOP_AFTER_SEEN = _int("STOP_AFTER_SEEN", 10)
MAX_POSTS_PER_GROUP = _int("MAX_POSTS_PER_GROUP", 0)  # 0 = không giới hạn

# --- Media ----------------------------------------------------------------
MEDIA_DIR = Path(_env("MEDIA_DIR", str(ROOT / "media")))
DOWNLOAD_MEDIA = _env("DOWNLOAD_MEDIA", "1") not in ("0", "false", "False")
DOWNLOAD_TIMEOUT_SEC = _int("DOWNLOAD_TIMEOUT_SEC", 30)
MAX_MEDIA_MB = _int("MAX_MEDIA_MB", 15)

# --- API ------------------------------------------------------------------
API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = _int("API_PORT", 8000)

DEBUG = _env("DEBUG", "0") not in ("0", "false", "False", "")

ROOT.mkdir(parents=True, exist_ok=True)
