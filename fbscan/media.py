"""Tải ảnh đính kèm của bài viết về MEDIA_DIR."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Optional

import requests
from facebook_gql_scraper import FacebookContext

from . import config

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]


def _ext_for(url: str, content_type: Optional[str]) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    path = url.split("?")[0]
    suffix = Path(path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return suffix
    return ".jpg"


def download(
    ctx: FacebookContext,
    url: str,
    fb_post_id: str,
    index: int,
) -> Optional[dict]:
    """Tải 1 ảnh. Trả {file_path, bytes} hoặc None nếu thất bại."""
    if not url:
        return None
    folder = config.MEDIA_DIR / _safe(fb_post_id)
    folder.mkdir(parents=True, exist_ok=True)

    try:
        res = ctx.session.get(
            url,
            timeout=config.DOWNLOAD_TIMEOUT_SEC,
            stream=True,
            headers={"user-agent": _UA, "accept": "image/*,*/*;q=0.8"},
        )
        res.raise_for_status()
    except requests.RequestException:
        return None

    limit = config.MAX_MEDIA_MB * 1024 * 1024
    ext = _ext_for(url, res.headers.get("content-type"))
    target = folder / f"{index:02d}{ext}"

    total = 0
    try:
        with target.open("wb") as fh:
            for chunk in res.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise ValueError("file quá lớn")
                fh.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        return None

    if total < 512:  # ảnh lỗi/placeholder
        target.unlink(missing_ok=True)
        return None

    rel = target.relative_to(config.MEDIA_DIR).as_posix()
    return {"file_path": rel, "bytes": total, "sha": hashlib.md5(str(rel).encode()).hexdigest()}
