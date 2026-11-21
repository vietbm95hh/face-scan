"""Quét bài viết của group và ghi vào PostgreSQL."""

from __future__ import annotations

import random
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import facebook_gql_scraper as fgs

from . import config, db, media, normalize


@dataclass
class ScanResult:
    group_id: int
    group_name: str
    found: int = 0
    new: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)
    stopped_reason: str = ""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _sleep(delay: Optional[float] = None) -> None:
    if delay is None:
        delay = config.REQUEST_DELAY_SEC
    if delay > 0:
        time.sleep(delay * random.uniform(0.8, 1.4))


def scan_group(
    ctx: fgs.FacebookContext,
    group_ref: fgs.Group,
    *,
    trigger: str = "cli",
    max_posts: Optional[int] = None,
    max_age_days: Optional[int] = None,
    reset_cursor: bool = False,
    download_media: Optional[bool] = None,
) -> ScanResult:
    """Quét 1 group. group_ref là fgs.Group đã resolve."""
    max_posts = config.MAX_POSTS_PER_GROUP if max_posts is None else max_posts
    max_age_days = config.MAX_AGE_DAYS if max_age_days is None else max_age_days
    download_media = config.DOWNLOAD_MEDIA if download_media is None else download_media

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days > 0 else None

    # ---- metadata group -> DB
    with db.get_conn() as conn:
        gid = db.upsert_group(
            conn,
            str(group_ref.id),
            slug=None,
            name=_safe_attr(group_ref, "name"),
            privacy=_safe_attr(group_ref, "privacy"),
            member_count_txt=_safe_attr(group_ref, "member_count"),
            group_url=_safe_attr(group_ref, "url"),
        )
        if reset_cursor:
            db.reset_cursor(conn, gid)
        cursor_row = db.load_cursor(conn, gid)
        job_id = db.start_job(conn, gid, trigger)

    result = ScanResult(group_id=gid, group_name=_safe_attr(group_ref, "name") or str(group_ref.id))
    _log(f"\n=== [{gid}] {result.group_name} (fb={group_ref.id}) ===")

    it: Iterable = group_ref.get_posts()
    if cursor_row:
        try:
            it.thaw(
                fgs.FrozenPaginatedIterator(
                    iterator_type="group_timeline",
                    query_variables={"groupId": str(group_ref.id)},
                    remaining_raw=[],
                    cursor=cursor_row["cursor"],
                    # tên field theo facebook-gql-scraper: has_next_page
                    has_next_page=bool(cursor_row["has_next"]),
                )
            )
            _log(f"  -> tiếp tục từ cursor đã lưu")
        except ValueError as exc:  # state không còn hợp lệ -> quét lại từ đầu
            _log(f"  -> cursor cũ không dùng được ({exc}), quét lại từ đầu")
            cursor_row = None
            it = group_ref.get_posts()

    stopped_reason = "hết dữ liệu"

    try:
        seen_streak = 0
        with db.get_conn() as conn:
            for post in it:
                known = db.post_exists(conn, gid, str(post.id))
                if known:
                    result.updated += 1
                    seen_streak += 1
                else:
                    seen_streak = 0

                if seen_streak >= config.STOP_AFTER_SEEN:
                    stopped_reason = f"gặp {config.STOP_AFTER_SEEN} bài cũ liên tiếp"
                    break

                row = normalize.to_row(post)
                if cutoff and row["posted_at"] and row["posted_at"] < cutoff:
                    stopped_reason = f"bài cũ hơn {max_age_days} ngày"
                    break

                post_id, is_new = db.upsert_post(conn, gid, row)
                result.found += 1
                if is_new:
                    result.new += 1
                else:
                    result.updated += 1

                if download_media and row["_media"]:
                    saved = []
                    for m in row["_media"]:
                        got = media.download(ctx, m["source_url"], str(post.id), m["position"])
                        if got:
                            saved.append(
                                {
                                    **m,
                                    "file_path": got["file_path"],
                                    "bytes": got["bytes"],
                                    "downloaded_at": db.utcnow(),
                                }
                            )
                        _sleep(0.3)
                    db.replace_media(conn, post_id, saved)

                if result.found % 20 == 0:
                    _log(f"  ... {result.found} bài (mới {result.new}), lưu tạm")
                    conn.commit()

                if max_posts and result.found >= max_posts:
                    stopped_reason = f"đạt giới hạn {max_posts} bài"
                    break

                _sleep()

            # lưu cursor để lần sau chạy tiếp
            frozen = it.freeze()
            db.save_cursor(conn, gid, frozen.cursor, frozen.has_next_page)
            db.mark_group_scanned(conn, gid)
            db.finish_job(
                conn,
                job_id,
                status="done",
                found=result.found,
                new=result.new,
                updated=result.updated,
            )
    except Exception as exc:  # noqa: BLE001 - cần chạy tiếp group kế tiếp
        err = f"{type(exc).__name__}: {exc}"
        result.errors.append(err)
        stopped_reason = "lỗi"
        _log(f"  X Lỗi: {err}")
        if config.DEBUG:
            traceback.print_exc()
        with db.get_conn() as conn:
            db.finish_job(
                conn,
                job_id,
                status="error",
                found=result.found,
                new=result.new,
                updated=result.updated,
                error=err,
            )

    result.stopped_reason = stopped_reason
    _log(
        f"  OK: xong: {result.found} bài (mới {result.new}, cập nhật {result.updated}) "
        f"— dừng vì: {stopped_reason}"
    )
    return result


def scan_many(
    ctx: fgs.FacebookContext,
    groups: list[fgs.Group],
    **kwargs,
) -> list[ScanResult]:
    results = []
    for i, grp in enumerate(groups, 1):
        results.append(scan_group(ctx, grp, **kwargs))
        if i < len(groups):
            delay = random.uniform(config.GROUP_DELAY_MIN, config.GROUP_DELAY_MAX)
            _log(f"   nghỉ {delay:.0f}s trước group kế tiếp...")
            time.sleep(delay)
    return results


def _safe_attr(obj, name: str):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None
