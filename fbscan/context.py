"""Tạo FacebookContext (facebook-gql-scraper) và xử lý lỗi phiên đăng nhập."""

from __future__ import annotations

import facebook_gql_scraper as fgs

from . import config


class LoginRequired(RuntimeError):
    """Cookies hết hạn hoặc không hợp lệ."""


def make_context(*, quiet: bool = True) -> fgs.FacebookContext:
    if not config.COOKIES_FILE.exists():
        raise LoginRequired(
            f"Không thấy file cookies: {config.COOKIES_FILE}\n"
            "Export cookies Facebook bằng extension Cookie-Editor (JSON) "
            "và lưu đúng tên file này."
        )
    try:
        return fgs.FacebookContext(
            cookies_file=str(config.COOKIES_FILE),
            state_file=str(config.STATE_FILE),
            quiet=quiet,
        )
    except Exception as exc:  # JSON hỏng, thiếu cookie...
        raise LoginRequired(f"Không đọc được cookies: {exc}") from exc


def resolve_group(ctx, ref: str) -> fgs.Group:
    """ref có thể là group id (toàn chữ số) hoặc slug/groupname."""
    ref = ref.strip().strip("/")
    if ref.isdigit():
        return fgs.Group.from_group_id(ctx, group_id=ref)
    if "/groups/" in ref:  # link đầy đủ
        ref = ref.split("/groups/")[-1].split("?")[0].strip("/")
        if ref.isdigit():
            return fgs.Group.from_group_id(ctx, group_id=ref)
    return fgs.Group.from_groupname(ctx, groupname=ref)
