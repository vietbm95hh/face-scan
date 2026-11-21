"""Quét bài viết theo từ khóa bằng ô tìm kiếm Facebook.

Khác với `scanner.scan_group` (đọc feed của group), ở đây ta gọi
`SearchEngine.search(term, filter_type="post")` — chính là dữ liệu của trang
`facebook.com/search/posts/?q=<từ khóa>` (ô tìm kiếm trên Facebook), lấy
**mọi** bài khớp từ khóa: bài trong group, bài của Page, bài trang cá nhân.

Kết quả tìm kiếm đã chứa nội dung/ảnh/chỉ số tương tác nên được lưu ngay; nếu
thiếu nội dung (trường hợp hiếm) thì lấy chi tiết bằng `Post.from_post_id()`.

Nguồn của bài (`scan_posts.source_type`):

- `group` — bài trong group; group tự được thêm vào `scan_groups`, `group_id` được set.
- `page`  — bài của Page (vd facebook.com/CellphoneSVietnam/...)
- `user`  — bài trên trang cá nhân (vd facebook.com/angel.3182020/...)

Với `search_all=False` (mặc định, hành vi cũ) chỉ lưu bài thuộc group.
Với `search_all=True` lưu hết những gì ô tìm kiếm trả về.
"""

from __future__ import annotations

import random
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import facebook_gql_scraper as fgs

from . import config, db, media, normalize


@dataclass
class KeywordScanResult:
    keyword_id: int
    term: str
    found: int = 0          # số bài thấy trên trang tìm kiếm
    new: int = 0            # bài mới thêm vào DB
    updated: int = 0        # bài đã có -> cập nhật chỉ số
    fetched: int = 0        # số bài xử lý thành công
    failed: int = 0         # số bài lỗi (bỏ qua)
    skipped: int = 0        # số bài không thuộc group khi search_all=false
    by_source: dict = field(default_factory=lambda: {"group": 0, "page": 0, "user": 0})
    errors: list[str] = field(default_factory=list)
    stopped_reason: str = ""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _sleep(delay: Optional[float] = None) -> None:
    if delay is None:
        delay = config.REQUEST_DELAY_SEC
    if delay > 0:
        time.sleep(delay * random.uniform(0.8, 1.4))


# --------------------------------------------------------------- nguồn bài
_GROUP_URL_RE = re.compile(r"/groups/([^/?#]+)")
# facebook.com/<slug>/posts/..., facebook.com/<slug>/permalink/..., facebook.com/<slug>/videos/...
_SOURCE_URL_RE = re.compile(
    r"^/(?P<slug>[^/]+)/(?:posts|permalink|videos|photos|reel|reels)(?:/|$)"
)
# Đoạn đường dẫn không phải là trang cá nhân/Page
_RESERVED_PATHS = {
    "groups", "watch", "marketplace", "gaming", "search", "profile.php",
    "story.php", "permalink.php", "photo.php", "media", "events", "hashtag",
    "public", "reel", "stories", "share",
}


def _source_from_url(url: Optional[str]) -> Optional[dict]:
    """Đọc loại nguồn + id/username từ permalink của bài.

    Trả {"source_type": group|page|user, "source_fb_id", "source_url"} hoặc None.
    FB không nói rõ page hay cá nhân qua URL; coi mọi nguồn không phải group là
    `page` (gồm cả trang cá nhân) và giữ URL gốc để xem lại.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = parts.path or ""
    m = _GROUP_URL_RE.search(path)
    if m:
        return {"source_type": "group", "source_fb_id": m.group(1), "source_url": url}
    m = _SOURCE_URL_RE.match(path)
    if m:
        slug = m.group("slug")
        if slug.lower() not in _RESERVED_PATHS:
            return {"source_type": "page", "source_fb_id": slug, "source_url": url}
    return None


def _source_of(ctx, post: fgs.Post, url: Optional[str]) -> Optional[dict]:
    """Suy ra nguồn của bài: group (kèm fgs.Group) hoặc page/trang cá nhân."""
    src = _source_from_url(url)
    if src and src["source_type"] == "group":
        return {
            **src,
            "group": fgs.Group(ctx, {"id": src["source_fb_id"]}),
            "source_name": None,
        }

    try:
        grp = post.group
    except Exception:  # noqa: BLE001
        grp = None
    if grp is not None:
        return {
            "source_type": "group",
            "source_fb_id": str(grp.id),
            "source_url": _safe_attr(grp, "url") or url,
            "group": grp,
            "source_name": _safe_attr(grp, "name"),
        }

    if not src:
        return None
    author = None
    try:
        author = post.owner
    except Exception:  # noqa: BLE001
        pass
    return {
        **src,
        "group": None,
        "source_name": _safe_attr(author, "name"),
    }


# --------------------------------------------------------------- lưu bài
def _save_post(
    ctx,
    conn,
    post: fgs.Post,
    keyword_id: int,
    source: dict,
    *,
    download_media: bool,
) -> tuple[bool, int]:
    """Lưu 1 bài tìm được. Trả (is_new, số ảnh đã tải)."""
    gid = None
    if source["source_type"] == "group" and source.get("group") is not None:
        grp = source["group"]
        gid = db.upsert_group(
            conn,
            source["source_fb_id"],
            slug=None,
            name=_safe_attr(grp, "name") or source.get("source_name"),
            privacy=_safe_attr(grp, "privacy"),
            member_count_txt=_safe_attr(grp, "member_count"),
            group_url=_safe_attr(grp, "url") or source.get("source_url"),
        )

    row = normalize.to_row(post)
    # Gắn keyword_id + nguồn ngay từ lần ghi đầu; bài đã có thì giữ liên kết cũ.
    row.update(
        keyword_id=keyword_id,
        source_type=source["source_type"],
        source_fb_id=source["source_fb_id"],
        source_name=source.get("source_name"),
        source_url=source.get("source_url"),
    )
    post_id, is_new = db.upsert_post(conn, gid, row)

    saved = []
    if download_media and row["_media"]:
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
    return is_new, len(saved)


def _errors_text(result: KeywordScanResult) -> Optional[str]:
    """Tóm tắt lỗi từng bài để ghi vào scan_jobs.error (chỉ khi có lỗi)."""
    if not result.errors:
        return None
    head = result.errors[:5]
    more = len(result.errors) - len(head)
    text = "; ".join(head) + (f" (+{more} lỗi khác)" if more > 0 else "")
    return text[:2000]


def _needs_detail(post: fgs.Post) -> bool:
    """Kết quả search đôi khi thiếu nội dung -> phải lấy chi tiết."""
    try:
        return not post.text and not (post.creation_time or 0)
    except Exception:  # noqa: BLE001
        return True


def scan_keyword(
    ctx: fgs.FacebookContext,
    term: str,
    *,
    keyword_id: int,
    trigger: str = "keyword",
    max_posts: Optional[int] = None,
    max_age_days: Optional[int] = None,
    download_media: Optional[bool] = None,
    search_all: bool = False,
) -> KeywordScanResult:
    """Quét ô tìm kiếm Facebook theo `term`, lưu bài vào DB.

    search_all=True: lưu mọi bài tìm thấy (group + Page + trang cá nhân).
    """
    max_posts = config.MAX_POSTS_PER_GROUP if max_posts is None else max_posts
    max_age_days = config.MAX_AGE_DAYS if max_age_days is None else max_age_days
    download_media = config.DOWNLOAD_MEDIA if download_media is None else download_media

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days > 0 else None
    )

    result = KeywordScanResult(keyword_id=keyword_id, term=term)
    with db.get_conn() as conn:
        job_id = db.start_job(conn, None, trigger)

    scope = "mọi nguồn (group/Page/trang cá nhân)" if search_all else "chỉ bài trong group"
    _log(f"\n=== [từ khóa #{keyword_id}] \"{term}\" — {scope} ===")

    stopped_reason = "hết trang kết quả"
    try:
        it = fgs.SearchEngine(ctx).search(term, filter_type="post")

        with db.get_conn() as conn:
            for hit in it:
                result.found += 1

                # Bài cũ hơn mốc thời gian -> các bài sau cũng cũ (kết quả xếp mới trước).
                posted_at = normalize.to_ts(getattr(hit, "creation_time", None))
                if cutoff and posted_at and posted_at < cutoff:
                    stopped_reason = f"bài cũ hơn {max_age_days} ngày"
                    break
                if max_posts and result.fetched >= max_posts:
                    stopped_reason = f"đạt giới hạn {max_posts} bài"
                    break

                try:
                    post = hit
                    if _needs_detail(post):
                        post = fgs.Post.from_post_id(ctx, str(hit.id))

                    source = _source_of(ctx, post, post.url or hit.url)
                    if source is None:
                        if not search_all:
                            result.skipped += 1
                            continue
                        # Không đọc được nguồn -> lưu như bài trên Page/permalink gốc.
                        source = {
                            "source_type": "page",
                            "source_fb_id": str(post.id),
                            "source_url": post.url or str(hit.url),
                            "group": None,
                            "source_name": None,
                        }
                    if source["source_type"] != "group" and not search_all:
                        result.skipped += 1
                        _log(f"  - {hit.id}: không phải bài group, bỏ qua (search_all=false)")
                        continue

                    # Đã có trong DB (bất kỳ nguồn nào) -> chỉ cập nhật chỉ số tương tác.
                    if db.get_post_by_fbid(conn, str(post.id)):
                        result.updated += 1
                        _log(f"  - {post.id}: đã có trong DB")
                        continue

                    is_new, _n_media = _save_post(
                        ctx, conn, post, keyword_id, source, download_media=download_media
                    )
                except Exception as exc:  # noqa: BLE001 - 1 bài lỗi thì bỏ qua, chạy tiếp
                    result.failed += 1
                    msg = f"bài {hit.id}: {type(exc).__name__}: {exc}"
                    result.errors.append(msg)
                    _log(f"  X {msg}")
                    if config.DEBUG:
                        traceback.print_exc()
                else:
                    result.fetched += 1
                    if is_new:
                        result.new += 1
                        result.by_source[source["source_type"]] += 1
                    else:
                        result.updated += 1

                if result.fetched and result.fetched % 10 == 0:
                    _log(
                        f"  ... {result.fetched} bài (mới {result.new}), lưu tạm"
                    )
                    conn.commit()

                _sleep()

            db.finish_job(
                conn,
                job_id,
                status="done",
                found=result.found,
                new=result.new,
                updated=result.updated,
                error=_errors_text(result),
            )
    except Exception as exc:  # noqa: BLE001 - ghi lỗi vào job rồi báo cho worker
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
    bs = result.by_source
    _log(
        f"  OK: xong: {result.found} kết quả, lưu {result.fetched} "
        f"(mới {result.new}: {bs['group']} group, {bs['page']} page/trang cá nhân; "
        f"đã có {result.updated}, bỏ qua {result.skipped}, lỗi {result.failed}) "
        f"— dừng vì: {stopped_reason}"
    )
    return result


def _safe_attr(obj, name: str):
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None
