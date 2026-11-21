"""Chuẩn hóa đối tượng Post của facebook-gql-scraper thành dict ghi DB."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from facebook_gql_scraper import Post


def _ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def to_ts(value) -> Optional[datetime]:
    """Unix timestamp (giây) -> datetime UTC."""
    return _ts(value)


def _author(post: Post) -> tuple[Optional[str], Optional[str]]:
    owner = getattr(post, "owner", None)
    if owner is None:
        return None, None
    try:
        return owner.id, owner.name
    except Exception:
        return None, None


def _media_items(post: Post) -> list[dict]:
    """Trả list media thô: {kind, position, source_url, thumb_url}."""
    items: list[dict] = []
    try:
        attachments = post.attachments or []
    except Exception:
        return items

    for att in attachments:
        kind = type(att).__name__.lower()  # Photo, Video, GenericMedia, Sticker
        url = getattr(att, "image_url", None) or getattr(att, "thumbnail_image_url", None)
        if not url:
            url = getattr(att, "fallback_image_url", None)
        if not url:
            continue
        items.append(
            {
                "kind": kind,
                "position": len(items),
                "source_url": url,
                "thumb_url": getattr(att, "thumbnail_image_url", None),
            }
        )
    return items


def _permalink(post: Post) -> Optional[str]:
    url = getattr(post, "url", None)
    if url:
        return url
    try:
        grp = getattr(post, "group", None)
        if grp is not None:
            return f"https://www.facebook.com/groups/{grp.id}/posts/{post.id}"
    except Exception:
        pass
    return None


def make_title(text: Optional[str]) -> Optional[str]:
    """FB group post thường không có title -> lấy 120 ký tự đầu làm tiêu đề."""
    if not text:
        return None
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first_line = re.sub(r"\s+", " ", first_line)
    if not first_line:
        return None
    return first_line[:120] + ("..." if len(first_line) > 120 else "")


def to_row(post: Post) -> dict[str, Any]:
    """Post model -> dict khớp cột bảng scan_posts."""
    author_id, author_name = _author(post)
    try:
        text = post.text
    except Exception:
        text = None
    media = _media_items(post)

    return {
        "fb_post_id": str(post.id),
        "author_fb_id": author_id,
        "author_name": author_name,
        "title": post.title or make_title(text),
        "content": text,
        "permalink": _permalink(post),
        "posted_at": _ts(getattr(post, "creation_time", None)),
        "like_count": _to_int(getattr(post, "reaction_count", 0)),
        "comment_count": _to_int(getattr(post, "comments_count", 0)),
        "share_count": _to_int(getattr(post, "share_count", 0)),
        "reaction_breakdown": getattr(post, "top_reaction_breakdown", None) or [],
        "has_media": bool(media),
        "_media": media,
        "raw": None,
    }


def _to_int(value) -> int:
    """reaction_count có thể là '1.2K' / '47619' / int."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip().upper()
    try:
        if s.endswith("K"):
            return int(float(s[:-1].replace(",", "")) * 1_000)
        if s.endswith("M"):
            return int(float(s[:-1].replace(",", "")) * 1_000_000)
        if s.endswith("B"):
            return int(float(s[:-1].replace(",", "")) * 1_000_000_000)
        return int(float(s.replace(",", "")))
    except ValueError:
        return 0
