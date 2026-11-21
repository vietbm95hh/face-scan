"""Kết nối PostgreSQL + các thao tác DB cho scraper và API."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config


def _connect(**kwargs: Any) -> psycopg.Connection:
    """Mở kết nối, tự thử lại khi Tailscale/mạng chập chờn.

    `connect_timeout` áp dụng cho từng lần thử. Chỉ thử lại lỗi tầng kết nối
    (`OperationalError`); lỗi SQL/schema vẫn ném ra ngay.
    """
    kwargs.setdefault("connect_timeout", config.PG_CONNECT_TIMEOUT)
    attempts = max(1, config.PG_CONNECT_RETRIES)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg.connect(config.DATABASE_URL, **kwargs)
        except psycopg.OperationalError as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = config.PG_CONNECT_BACKOFF * attempt
            print(
                f"  ! PostgreSQL chưa kết nối được ({type(exc).__name__}), "
                f"thử lại {attempt}/{attempts - 1} sau {delay:.1f}s...",
                flush=True,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def connect(**kwargs: Any) -> psycopg.Connection:
    """Mở kết nối mới tới database fbscan (địa chỉ lấy từ .env, thường là server dev)."""
    return _connect(**kwargs)


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Context manager: tự commit/rollback."""
    conn = _connect(row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ groups
def upsert_group(conn: psycopg.Connection, fb_group_id: str, **fields: Any) -> int:
    """Chèn hoặc cập nhật group, trả về id nội bộ."""
    row = conn.execute(
        """
        INSERT INTO scan_groups (fb_group_id, slug, name, privacy, member_count_txt, group_url, avatar_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fb_group_id) DO UPDATE SET
            slug             = COALESCE(EXCLUDED.slug, scan_groups.slug),
            name             = CASE WHEN EXCLUDED.name LIKE 'Group %%' THEN scan_groups.name ELSE EXCLUDED.name END,
            privacy          = COALESCE(EXCLUDED.privacy, scan_groups.privacy),
            member_count_txt = COALESCE(EXCLUDED.member_count_txt, scan_groups.member_count_txt),
            group_url        = COALESCE(EXCLUDED.group_url, scan_groups.group_url),
            avatar_path      = COALESCE(EXCLUDED.avatar_path, scan_groups.avatar_path)
        RETURNING id
        """,
        (
            fb_group_id,
            fields.get("slug"),
            fields.get("name") or f"Group {fb_group_id}",
            fields.get("privacy"),
            fields.get("member_count_txt"),
            fields.get("group_url"),
            fields.get("avatar_path"),
        ),
    ).fetchone()
    return int(row["id"])


def list_groups(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT * FROM scan_groups ORDER BY is_active DESC, name"
    ).fetchall()


def get_group(conn: psycopg.Connection, group_id: int) -> Optional[dict]:
    return conn.execute("SELECT * FROM scan_groups WHERE id=%s", (group_id,)).fetchone()


def mark_group_scanned(conn: psycopg.Connection, group_id: int) -> None:
    conn.execute("UPDATE scan_groups SET last_scanned_at=now() WHERE id=%s", (group_id,))


# ------------------------------------------------------------------ cursors
def load_cursor(conn: psycopg.Connection, group_id: int) -> Optional[dict]:
    return conn.execute(
        "SELECT cursor, has_next FROM scan_cursors WHERE group_id=%s", (group_id,)
    ).fetchone()


def save_cursor(
    conn: psycopg.Connection, group_id: int, cursor: Optional[str], has_next: bool
) -> None:
    conn.execute(
        """
        INSERT INTO scan_cursors (group_id, cursor, has_next, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (group_id) DO UPDATE SET
            cursor=EXCLUDED.cursor, has_next=EXCLUDED.has_next, updated_at=now()
        """,
        (group_id, cursor, has_next),
    )


def reset_cursor(conn: psycopg.Connection, group_id: int) -> None:
    conn.execute("DELETE FROM scan_cursors WHERE group_id=%s", (group_id,))


# -------------------------------------------------------------------- posts
def upsert_post(
    conn: psycopg.Connection, group_id: Optional[int], post: dict
) -> tuple[int, bool]:
    """Insert/update 1 bài. Trả về (post_id, is_new).

    `group_id=None` = bài không thuộc group (Page / trang cá nhân, quét ô tìm kiếm).
    Bài cũ (trước migration 005) vẫn có `group_id` nên dedupe theo
    `(group_id, fb_post_id)` như cũ; bài group_id NULL dedupe theo `fb_post_id`.
    """
    if group_id is None:
        existing = get_post_by_fbid(conn, str(post["fb_post_id"]))
        if existing:
            return _update_post(conn, existing["id"], post), False
    row = conn.execute(
        """
        INSERT INTO scan_posts (
            fb_post_id, group_id, keyword_id, source_type, source_fb_id, source_name,
            source_url, author_fb_id, author_name, title, content, permalink,
            posted_at, like_count, comment_count, share_count, reaction_breakdown,
            has_media, scraped_at, updated_at, raw
        ) VALUES (
            %(fb_post_id)s, %(group_id)s, %(keyword_id)s, %(source_type)s, %(source_fb_id)s,
            %(source_name)s, %(source_url)s, %(author_fb_id)s, %(author_name)s, %(title)s,
            %(content)s, %(permalink)s, %(posted_at)s, %(like_count)s, %(comment_count)s,
            %(share_count)s, %(reaction_breakdown)s, %(has_media)s, now(), now(), %(raw)s
        )
        ON CONFLICT (group_id, fb_post_id) DO UPDATE SET
            keyword_id         = COALESCE(scan_posts.keyword_id, EXCLUDED.keyword_id),
            source_type        = COALESCE(NULLIF(scan_posts.source_type, 'group'), EXCLUDED.source_type),
            source_fb_id       = COALESCE(scan_posts.source_fb_id, EXCLUDED.source_fb_id),
            source_name        = COALESCE(scan_posts.source_name, EXCLUDED.source_name),
            source_url         = COALESCE(scan_posts.source_url, EXCLUDED.source_url),
            author_fb_id       = COALESCE(EXCLUDED.author_fb_id, scan_posts.author_fb_id),
            author_name        = COALESCE(EXCLUDED.author_name, scan_posts.author_name),
            title              = COALESCE(EXCLUDED.title, scan_posts.title),
            content            = COALESCE(EXCLUDED.content, scan_posts.content),
            permalink          = COALESCE(EXCLUDED.permalink, scan_posts.permalink),
            posted_at          = COALESCE(EXCLUDED.posted_at, scan_posts.posted_at),
            like_count         = EXCLUDED.like_count,
            comment_count      = EXCLUDED.comment_count,
            share_count        = EXCLUDED.share_count,
            reaction_breakdown = EXCLUDED.reaction_breakdown,
            updated_at         = now()
        RETURNING id, (xmax = 0) AS is_new
        """,
        {
            **_post_params(post),
            "group_id": group_id,
        },
    ).fetchone()
    return int(row["id"]), bool(row["is_new"])


def _post_params(post: dict) -> dict:
    return {
        **post,
        "keyword_id": post.get("keyword_id"),
        "source_type": post.get("source_type") or "group",
        "source_fb_id": post.get("source_fb_id"),
        "source_name": post.get("source_name"),
        "source_url": post.get("source_url"),
        "reaction_breakdown": Jsonb(post.get("reaction_breakdown") or []),
        "raw": Jsonb(post.get("raw")) if post.get("raw") is not None else None,
    }


def _update_post(conn: psycopg.Connection, post_id: int, post: dict) -> int:
    """Cập nhật chỉ số/nội dung của bài đã có (giữ nguyên group_id, keyword_id, nguồn)."""
    conn.execute(
        """
        UPDATE scan_posts SET
            author_fb_id       = COALESCE(%(author_fb_id)s, author_fb_id),
            author_name        = COALESCE(%(author_name)s, author_name),
            title              = COALESCE(%(title)s, title),
            content            = COALESCE(%(content)s, content),
            permalink          = COALESCE(%(permalink)s, permalink),
            posted_at          = COALESCE(%(posted_at)s, posted_at),
            like_count         = %(like_count)s,
            comment_count      = %(comment_count)s,
            share_count        = %(share_count)s,
            reaction_breakdown = %(reaction_breakdown)s,
            updated_at         = now()
        WHERE id = %(post_id)s
        """,
        {**_post_params(post), "post_id": post_id},
    )
    return post_id


def get_post_by_fbid(conn: psycopg.Connection, fb_post_id: str) -> Optional[dict]:
    """Tìm bài theo fb_post_id (bài có thể nằm ở bất kỳ group nào)."""
    return conn.execute(
        "SELECT id, group_id FROM scan_posts WHERE fb_post_id=%s ORDER BY id LIMIT 1",
        (fb_post_id,),
    ).fetchone()


def get_post_by_source_path(conn: psycopg.Connection, source_path: str) -> Optional[dict]:
    """Tìm bài theo đường dẫn Facebook đã chuẩn hoá (<chủ thể>/posts/<id>).

    Bài lấy qua browser (migration 007) được gán fb_post_id giả nên cách tra này là
    nguồn dedupe chính cho chúng.
    """
    if not source_path:
        return None
    return conn.execute(
        "SELECT id, group_id FROM scan_posts WHERE source_path=%s ORDER BY id LIMIT 1",
        (source_path,),
    ).fetchone()


def insert_browser_post(
    conn: psycopg.Connection, group_id: Optional[int], post: dict
) -> tuple[int, bool]:
    """Lưu 1 bài đọc từ trang tìm kiếm qua browser. Trả (post_id, is_new).

    Không đủ dữ liệu GraphQL nên chỉ ghi những cột chắc chắn: nội dung, tác giả, ngày,
    link, nguồn (`source_type/source_fb_id/source_name`) và `source_path` để lần sau
    nhận ra bài đã có.
    """
    existing = get_post_by_source_path(conn, post.get("source_path") or "")
    if existing is None:
        existing = get_post_by_fbid(conn, str(post["fb_post_id"]))
    if existing:
        conn.execute(
            """
            UPDATE scan_posts SET
                content     = COALESCE(content, %s),
                permalink   = COALESCE(permalink, %s),
                posted_at   = COALESCE(posted_at, %s),
                author_name = COALESCE(author_name, %s),
                source_name = COALESCE(source_name, %s),
                updated_at  = now()
            WHERE id=%s
            """,
            (
                post.get("content"),
                post.get("permalink"),
                post.get("posted_at"),
                post.get("author_name"),
                post.get("source_name"),
                existing["id"],
            ),
        )
        return int(existing["id"]), False

    row = conn.execute(
        """
        INSERT INTO scan_posts (
            fb_post_id, group_id, keyword_id, source_type, source_fb_id, source_name,
            source_url, source_path, author_name, title, content, permalink, posted_at,
            has_media, fetched_by, scraped_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        RETURNING id
        """,
        (
            str(post["fb_post_id"]),
            group_id,
            post.get("keyword_id"),
            post.get("source_type") or "unknown",
            post.get("source_fb_id"),
            post.get("source_name"),
            post.get("source_url") or post.get("permalink"),
            post.get("source_path"),
            post.get("author_name"),
            post.get("title"),
            post.get("content"),
            post.get("permalink"),
            post.get("posted_at"),
            bool(post.get("has_media")),
            post.get("fetched_by") or "browser",
        ),
    ).fetchone()
    return int(row["id"]), True


def get_post_by_fbid_full(conn: psycopg.Connection, fb_post_id: str) -> Optional[dict]:
    """Tìm bài theo fb_post_id, trả cả id/group_id/posted_at/permalink (để so sánh)."""
    return conn.execute(
        """
        SELECT id, group_id, fb_post_id, posted_at, permalink, title, source_type,
               source_fb_id, source_name, like_count, comment_count, share_count
        FROM scan_posts WHERE fb_post_id=%s ORDER BY id LIMIT 1
        """,
        (fb_post_id,),
    ).fetchone()


def post_exists(conn: psycopg.Connection, group_id: int, fb_post_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM scan_posts WHERE group_id=%s AND fb_post_id=%s",
        (group_id, fb_post_id),
    ).fetchone()
    return row is not None


def replace_media(conn: psycopg.Connection, post_id: int, media: list[dict]) -> None:
    """Ghi đè danh sách media của 1 bài."""
    conn.execute("DELETE FROM scan_post_media WHERE post_id=%s", (post_id,))
    for m in media:
        conn.execute(
            """
            INSERT INTO scan_post_media
                (post_id, kind, position, source_url, file_path, thumb_url, width, height, bytes, downloaded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                post_id,
                m.get("kind", "photo"),
                m.get("position", 0),
                m.get("source_url"),
                m.get("file_path"),
                m.get("thumb_url"),
                m.get("width"),
                m.get("height"),
                m.get("bytes"),
                m.get("downloaded_at"),
            ),
        )


# --------------------------------------------------------------------- jobs
def start_job(conn: psycopg.Connection, group_id: Optional[int], trigger: str) -> int:
    row = conn.execute(
        """
        INSERT INTO scan_jobs (group_id, status, trigger_source, started_at)
        VALUES (%s, 'running', %s, now()) RETURNING id
        """,
        (group_id, trigger),
    ).fetchone()
    return int(row["id"])


def finish_job(
    conn: psycopg.Connection,
    job_id: int,
    *,
    status: str,
    found: int = 0,
    new: int = 0,
    updated: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE scan_jobs SET status=%s, finished_at=now(), posts_found=%s,
            posts_new=%s, posts_updated=%s, error=%s
        WHERE id=%s
        """,
        (status, found, new, updated, error, job_id),
    )


def dump_state(path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- keywords
# Từ khóa của ô tìm kiếm Facebook. Chuẩn hoá: bỏ khoảng trắng thừa + lower
# để "Tuyển Dụng" và "tuyển  dụng" là một từ khóa.
def normalize_term(term: str) -> tuple[str, str]:
    """Trả (term hiển thị, term_key để tra DB)."""
    term = " ".join((term or "").split())
    return term, term.lower()


def upsert_keyword(conn: psycopg.Connection, term: str) -> dict:
    display, key = normalize_term(term)
    if not key:
        raise ValueError("Từ khóa rỗng")
    conn.execute(
        """
        INSERT INTO scan_keywords (term, term_key) VALUES (%s, %s)
        ON CONFLICT (term_key) DO NOTHING
        """,
        (display, key),
    )
    return conn.execute(
        "SELECT * FROM scan_keywords WHERE term_key=%s", (key,)
    ).fetchone()


def get_keyword(conn: psycopg.Connection, keyword_id: int) -> Optional[dict]:
    return conn.execute("SELECT * FROM scan_keywords WHERE id=%s", (keyword_id,)).fetchone()


def list_keywords(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT k.*,
               (SELECT count(*) FROM scan_posts p WHERE p.keyword_id=k.id)::int AS post_count,
               (SELECT max(scraped_at) FROM scan_posts p WHERE p.keyword_id=k.id) AS last_scraped_at
        FROM scan_keywords k
        ORDER BY k.term_key
        """
    ).fetchall()


# ----------------------------------------------------------------- requests
# Hàng đợi "quét group X trong N ngày / tối đa M bài" (keyword_id NULL) hoặc
# "quét từ khóa K trên ô tìm kiếm Facebook" (group_id NULL) do API tạo,
# CLI/worker thực thi.
def create_request(
    conn: psycopg.Connection,
    group_id: Optional[int],
    *,
    keyword_id: Optional[int] = None,
    max_posts: int = 0,
    days: int = 0,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    reset_cursor: bool = False,
    download_media: bool = True,
    search_all: bool = False,
    keyword_match: str = "strict",
    sort: str = "recent",
    exclusions: Optional[list[str]] = None,
    excluded_terms: Optional[list[str]] = None,
    exclude_sources: Optional[list[str]] = None,
    scope_key: Optional[str] = None,
    requested_by: str = "api",
    note: Optional[str] = None,
) -> dict:
    return conn.execute(
        """
        INSERT INTO scan_requests
            (group_id, keyword_id, max_posts, days, date_from, date_to, reset_cursor,
             download_media, search_all, keyword_match, sort, exclusions, excluded_terms,
             exclude_sources, scope_key, requested_by, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            group_id,
            keyword_id,
            max_posts,
            days,
            date_from,
            date_to,
            reset_cursor,
            download_media,
            search_all,
            keyword_match or "strict",
            sort or "recent",
            list(exclusions or []),
            list(excluded_terms or []),
            list(exclude_sources or []),
            scope_key,
            requested_by,
            note,
        ),
    ).fetchone()


_REQUEST_SELECT = """
    SELECT r.*, g.name AS group_name, g.fb_group_id, k.term AS keyword,
           (SELECT count(*) FROM scan_reviews v WHERE v.request_id = r.id)::int AS review_count
    FROM scan_requests r
    LEFT JOIN scan_groups g ON g.id = r.group_id
    LEFT JOIN scan_keywords k ON k.id = r.keyword_id
"""


def list_requests(
    conn: psycopg.Connection,
    *,
    status: Optional[str] = None,
    group_id: Optional[int] = None,
    keyword_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    where, params = [], []
    if status:
        where.append("r.status = %s")
        params.append(status)
    if group_id:
        where.append("r.group_id = %s")
        params.append(group_id)
    if keyword_id:
        where.append("r.keyword_id = %s")
        params.append(keyword_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        _REQUEST_SELECT + f"{clause} ORDER BY r.id DESC LIMIT %s",
        tuple(params) + (limit,),
    ).fetchall()


def get_request(conn: psycopg.Connection, request_id: int) -> Optional[dict]:
    return conn.execute(
        _REQUEST_SELECT + "WHERE r.id = %s",
        (request_id,),
    ).fetchone()


def cancel_request(conn: psycopg.Connection, request_id: int) -> Optional[dict]:
    """Huỷ yêu cầu còn ở trạng thái queued (đang chạy thì phải dừng worker)."""
    return conn.execute(
        """
        UPDATE scan_requests
        SET status='canceled', finished_at=now(), error='huỷ bởi người dùng'
        WHERE id=%s AND status='queued'
        RETURNING *
        """,
        (request_id,),
    ).fetchone()


def claim_request(conn: psycopg.Connection, request_id: Optional[int] = None) -> Optional[dict]:
    """Lấy 1 yêu cầu queued và đánh dấu running (SKIP LOCKED: nhiều worker không trùng)."""
    sql = """
        UPDATE scan_requests r
        SET status='running', started_at=now()
        WHERE r.id = (
            SELECT id FROM scan_requests
            WHERE status='queued' {cond}
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING r.*
    """
    if request_id:
        row = conn.execute(sql.format(cond="AND id = %s"), (request_id,)).fetchone()
    else:
        row = conn.execute(sql.format(cond="")).fetchone()
    return row


def finish_request(
    conn: psycopg.Connection,
    request_id: int,
    *,
    status: str,
    job_id: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE scan_requests
        SET status=%s, job_id=COALESCE(%s, job_id), finished_at=now(), error=%s
        WHERE id=%s
        """,
        (status, job_id, error, request_id),
    )


def pending_request_count(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE status='queued')::int  AS queued,
               count(*) FILTER (WHERE status='running')::int AS running
        FROM scan_requests
        """
    ).fetchone()
    return {"queued": row["queued"], "running": row["running"]}


def list_covered_requests(conn: psycopg.Connection, scope_key: str) -> list[dict]:
    """Các lần quét đã xong cho cùng scope (để biết năm/ngày nào đã quét)."""
    return conn.execute(
        """
        SELECT id, status, date_from, date_to, days, max_posts, created_at, finished_at
        FROM scan_requests
        WHERE scope_key=%s AND status='done' AND finished_at IS NOT NULL
        ORDER BY finished_at DESC
        """,
        (scope_key,),
    ).fetchall()


# ------------------------------------------------------------------ reviews
REVIEW_SKIP_REASONS = {
    "already_in_db": "Đã có trong DB (không tải lại)",
    "duplicate": "Trùng trong cùng lượt quét",
    "out_of_range": "Ngoài khoảng thời gian lọc",
    "excluded_id": "Đã bị loại trừ trước đó",
    "excluded_term": "Chứa từ khóa loại trừ",
    "excluded_source": "Nguồn bị loại trừ",
    "not_group": "Không phải bài trong group (đang chỉ lấy bài group)",
    "not_match": "Nội dung không chứa từ khóa",
    "no_source": "Không xác định được nguồn bài",
    "error": "Lỗi khi lấy chi tiết bài",
}


def save_review(conn: psycopg.Connection, review: dict) -> int:
    row = conn.execute(
        """
        INSERT INTO scan_reviews
            (request_id, keyword_id, group_id, term, date_from, date_to, found,
             in_range, new_ids, existing_ids, skipped, search_all, created_by)
        VALUES (%(request_id)s, %(keyword_id)s, %(group_id)s, %(term)s, %(date_from)s,
                %(date_to)s, %(found)s, %(in_range)s, %(new_ids)s, %(existing_ids)s,
                %(skipped)s, %(search_all)s, %(created_by)s)
        RETURNING id
        """,
        {
            **review,
            "new_ids": list(review.get("new_ids") or []),
            "existing_ids": list(review.get("existing_ids") or []),
            "skipped": Jsonb(list(review.get("skipped") or [])),
        },
    ).fetchone()
    return int(row["id"])


def get_review(conn: psycopg.Connection, review_id: int) -> Optional[dict]:
    return conn.execute("SELECT * FROM scan_reviews WHERE id=%s", (review_id,)).fetchone()


def list_reviews(
    conn: psycopg.Connection, *, keyword_id: Optional[int] = None, limit: int = 50
) -> list[dict]:
    if keyword_id:
        return conn.execute(
            """
            SELECT r.*, COALESCE(k.term, r.term) AS term FROM scan_reviews r
            LEFT JOIN scan_keywords k ON k.id=r.keyword_id
            WHERE r.keyword_id=%s ORDER BY r.id DESC LIMIT %s
            """,
            (keyword_id, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT r.*, COALESCE(k.term, r.term) AS term FROM scan_reviews r
        LEFT JOIN scan_keywords k ON k.id=r.keyword_id
        ORDER BY r.id DESC LIMIT %s
        """,
        (limit,),
    ).fetchall()


def requeue_stale_requests(conn: psycopg.Connection) -> int:
    """Yêu cầu 'running' do worker chết giữa chừng -> trả về queued."""
    row = conn.execute(
        """
        UPDATE scan_requests SET status='queued', started_at=NULL,
            error='worker dừng giữa chừng, đã đưa lại hàng đợi'
        WHERE status='running' AND started_at < now() - interval '30 minutes'
        RETURNING id
        """
    ).fetchall()
    return len(row)


# ------------------------------------------------------------- page cursors
# Mốc quét feed Page: bài mới nhất đã quét xong. Lần sau chỉ cần quét phần
# mới hơn mốc này (feed Page xếp mới -> cũ nên gặp bài cũ hơn là dừng được).
def get_page_cursor(conn: psycopg.Connection, source_key: str) -> Optional[dict]:
    return conn.execute(
        "SELECT * FROM scan_page_cursors WHERE source_key=%s", (source_key,)
    ).fetchone()


def save_page_cursor(
    conn: psycopg.Connection,
    source_key: str,
    *,
    page_url: Optional[str] = None,
    page_name: Optional[str] = None,
    last_posted_at: Optional[datetime] = None,
    last_fb_post_id: Optional[str] = None,
    found: int = 0,
    new: int = 0,
    note: Optional[str] = None,
) -> dict:
    """Ghi/cập nhật mốc quét. Mốc thời gian chỉ TIẾN — quét lại ngày cũ không làm lùi mốc."""
    return conn.execute(
        """
        INSERT INTO scan_page_cursors
            (source_key, page_url, page_name, last_posted_at, last_fb_post_id,
             last_scan_at, last_found, last_new, note)
        VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s)
        ON CONFLICT (source_key) DO UPDATE SET
            page_url        = COALESCE(EXCLUDED.page_url, scan_page_cursors.page_url),
            page_name       = COALESCE(EXCLUDED.page_name, scan_page_cursors.page_name),
            last_posted_at  = GREATEST(
                scan_page_cursors.last_posted_at, EXCLUDED.last_posted_at
            ),
            last_fb_post_id = CASE
                WHEN EXCLUDED.last_posted_at IS NOT NULL
                 AND EXCLUDED.last_posted_at >= COALESCE(
                     scan_page_cursors.last_posted_at, EXCLUDED.last_posted_at)
                THEN EXCLUDED.last_fb_post_id
                ELSE scan_page_cursors.last_fb_post_id
            END,
            last_scan_at    = now(),
            last_found      = EXCLUDED.last_found,
            last_new        = EXCLUDED.last_new,
            note            = COALESCE(EXCLUDED.note, scan_page_cursors.note)
        RETURNING *
        """,
        (
            source_key,
            page_url,
            page_name,
            last_posted_at,
            last_fb_post_id,
            found,
            new,
            note,
        ),
    ).fetchone()


def delete_page_cursor(conn: psycopg.Connection, source_key: str) -> bool:
    row = conn.execute(
        "DELETE FROM scan_page_cursors WHERE source_key=%s RETURNING id", (source_key,)
    ).fetchone()
    return bool(row)
