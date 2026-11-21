
from __future__ import annotations

import datetime as _dt
import mimetypes
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import psycopg
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from fbscan import __version__, config, db, dtrange

app = FastAPI(
    title="FB Group Scanner API",
    version=__version__,
    description=(
        "API đọc dữ liệu bài viết Facebook group đã quét, "
        "và đặt yêu cầu quét theo group + khoảng thời gian."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(psycopg.OperationalError)
def _db_unavailable(_request: Request, exc: psycopg.OperationalError) -> JSONResponse:
    """DB rớt/timeout -> trả 503 rõ ràng thay vì 500 + traceback."""
    print(f"DB unavailable: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "Không kết nối được PostgreSQL "
                f"({config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}). "
                "Kiểm tra Tailscale/mạng tới server rồi thử lại."
            )
        },
    )


# ---------------------------------------------------------- edit models (UI)
class GroupUpdateIn(BaseModel):
    name: Optional[str] = Field(None, max_length=300)
    slug: Optional[str] = Field(None, max_length=300)
    is_active: Optional[bool] = None
    privacy: Optional[str] = Field(None, max_length=120)
    member_count_txt: Optional[str] = Field(None, max_length=120)
    group_url: Optional[str] = Field(None, max_length=1000)


class PostUpdateIn(BaseModel):
    is_read: Optional[bool] = None
    note: Optional[str] = Field(None, max_length=2000)
    updated_by: str = Field("ui", max_length=60)


class IdsIn(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=500)


# ------------------------------------------------------- scan request models
class DateFilterIn(BaseModel):
    """Lọc thời gian: theo NĂM hoặc theo NGÀY (không trộn). Bỏ trống hết = không lọc."""

    days: int = Field(
        0,
        ge=0,
        le=3650,
        description="Cách khác: chỉ lấy bài trong N ngày gần nhất. 0 = không dùng. Không dùng chung với năm/ngày.",
    )
    from_year: Optional[int] = Field(
        None, ge=dtrange.MIN_YEAR, le=2100, description="Từ năm (vd 2025). Bỏ trống đến năm = chỉ năm đó."
    )
    to_year: Optional[int] = Field(
        None, ge=dtrange.MIN_YEAR, le=2100, description="Đến năm (bao gồm cả năm này)."
    )
    from_date: Optional[str] = Field(None, description="Từ ngày YYYY-MM-DD (tính cả ngày này, giờ UTC).")
    to_date: Optional[str] = Field(None, description="Đến ngày YYYY-MM-DD (tính cả ngày này).")
    date: Optional[str] = Field(
        None,
        description=(
            "Chỉ lấy bài trong ĐÚNG MỘT NGÀY (YYYY-MM-DD) — gõ tắt của from_date=to_date. "
            'Gửi date="" (hoặc "today") = HÔM NAY theo giờ Việt Nam; bỏ hẳn field = không lọc ngày.'
        ),
    )
    lookback: Optional[int] = Field(
        None,
        ge=0,
        le=20000,
        description=(
            "Đọc tối đa bao nhiêu bài của kết quả tìm kiếm (kể cả bài ngoài khoảng) rồi dừng. "
            "0/trống = mặc định 1000. Kết quả Facebook xếp mới trước nên lọc năm cũ cần đọc sâu."
        ),
    )

    def to_range(self) -> dtrange.DateRange:
        """Khoảng ngày từ năm/ngày (ném ValueError nếu sai hoặc trộn với `days`)."""
        # date=None -> không lọc ngày; date="" hoặc "today" -> HÔM NAY (giờ VN)
        if self.date is not None:
            if self.from_date or self.to_date or self.from_year or self.to_year:
                raise ValueError("Chỉ dùng 'date' (1 ngày) HOẶC from_date/to_date/from_year, không gửi cả hai")
            raw = str(self.date).strip().lower()
            if raw in ("", "today", "homnay", "hôm nay", "now"):
                day = _today_vn().isoformat()  # date="" của UI = hôm nay
            else:
                day = raw
            return dtrange.build_range(from_date=day, to_date=day)
        rng = dtrange.build_range(
            from_year=self.from_year,
            to_year=self.to_year,
            from_date=self.from_date,
            to_date=self.to_date,
        )
        if self.days and not rng.empty:
            raise ValueError("Chỉ chọn 'days' HOẶC từ năm/ngày, không gửi cả hai")
        if self.days:
            return dtrange.DateRange(
                start=datetime.now(timezone.utc) - timedelta(days=self.days), end=None
            )
        return rng


class ScanRequestIn(DateFilterIn):
    group_id: Optional[int] = Field(None, description="id trong bảng scan_groups")
    group: Optional[str] = Field(None, description="fb_group_id / slug / link nếu chưa có trong DB")
    keyword: Optional[str] = Field(
        None,
        min_length=1,
        max_length=200,
        description="Từ khóa quét qua ô tìm kiếm Facebook (thay cho group_id/group).",
    )
    days: int = Field(
        0,
        ge=0,
        le=365,
        description=(
            "Chỉ lấy bài trong N ngày gần nhất. 0 = không giới hạn (dùng MAX_AGE_DAYS). "
            "Không dùng chung với from_year/from_date (dùng to_range để kiểm tra)."
        ),
    )
    max_posts: int = Field(0, ge=0, le=5000, description="Tối đa số bài. 0 = không giới hạn.")
    reset_cursor: bool = Field(
        True,
        description=(
            "Áp dụng cho quét theo group: bỏ cursor đã lưu để quét lại từ đầu. "
            "Quét theo từ khóa luôn đọc trang tìm kiếm từ đầu nên bỏ qua field này."
        ),
    )
    download_media: bool = Field(True, description="Tải ảnh đính kèm.")
    search_all: bool = Field(
        False,
        description=(
            "Chỉ dùng với keyword: true = lấy mọi bài ô tìm kiếm trả về "
            "(group + Page + trang cá nhân), false = chỉ bài trong group."
        ),
    )
    keyword_match: Literal["strict", "normal", "loose", "off"] = Field(
        "strict",
        description=(
            "Chỉ lưu bài có từ khóa trong TIÊU ĐỀ/NỘI DUNG. strict (mặc định) = đúng cụm / "
            "đủ mọi từ / chứa một cụm của từ khóa; normal = thêm bài chỉ trùng 1 từ chính; "
            "loose = thêm cả trùng 1 từ; off = không kiểm tra."
        ),
    )
    sort: Literal["recent", "relevance"] = Field(
        "recent",
        description=(
            "recent (mặc định) = lấy bài MỚI NHẤT trước (filter 'Bài viết mới đây' của FB, "
            "cần để lọc theo ngày); relevance = thứ tự liên quan mặc định của Facebook."
        ),
    )
    engine: Literal["auto", "graphql", "browser"] = Field(
        "auto",
        description=(
            "Cách lấy kết quả tìm kiếm: auto = GraphQL, nếu Facebook chặn (error 1357004) thì "
            "tự đọc trang search bằng Chromium; graphql = chỉ GraphQL; browser = luôn dùng browser."
        ),
    )
    exclusions: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description="Danh sách fb_post_id không lưu lại (bài đã review và loại trừ).",
    )
    excluded_terms: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Bỏ qua bài có chứa 1 trong các cụm từ này (không phân biệt hoa thường).",
    )
    exclude_sources: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Bỏ qua bài từ Page/trang cá nhân có source_fb_id này (vd 'shopdienmay').",
    )
    note: Optional[str] = Field(None, max_length=200)
    requested_by: str = Field("api", max_length=60)


class PreviewIn(DateFilterIn):
    """Kiểm tra trước: Facebook trả về gì, bài nào sẽ lưu / bị bỏ qua. Không ghi DB."""

    keyword: Optional[str] = Field(
        None, min_length=1, max_length=200, description="Từ khóa tìm trên ô tìm kiếm Facebook."
    )
    page: Optional[str] = Field(
        None,
        max_length=500,
        description="Link/tên Page cần quét feed (vd 'thanhnien' hoặc 'facebook.com/thanhnien'). "
        "Gửi field này thay cho 'keyword'.",
    )
    max_posts: int = Field(0, ge=0, le=5000, description="Tối đa số bài mới sẽ lưu. 0 = không giới hạn.")
    search_all: bool = Field(False, description="true = lấy mọi nguồn, false = chỉ bài group.")
    keyword_match: Literal["strict", "normal", "loose", "off"] = Field(
        "strict", description="Mức khớp từ khóa trong tiêu đề/nội dung (xem POST /api/scan)."
    )
    sort: Literal["recent", "relevance"] = Field(
        "recent", description="recent = bài mới nhất trước (cần để lọc theo ngày); relevance = mặc định FB."
    )
    engine: Literal["auto", "graphql", "browser"] = Field(
        "auto", description="auto = GraphQL rồi tự chuyển sang browser khi bị chặn; browser = luôn đọc trang."
    )
    sample: int = Field(30, ge=1, le=200, description="Số bài trả về trong `items` để xem.")
    resume: bool = Field(
        True,
        description="Chỉ với 'page': quét tiếp từ mốc lần quét trước (bỏ qua bài đã quét). "
        "false = quét lại toàn bộ khoảng đã chọn.",
    )
    exclusions: list[str] = Field(default_factory=list, max_length=1000)
    excluded_terms: list[str] = Field(default_factory=list, max_length=50)
    exclude_sources: list[str] = Field(default_factory=list, max_length=200)


class ReviewIn(DateFilterIn):
    """Chạy tìm kiếm thật + ghi `scan_reviews` để đối chiếu với Facebook."""

    keyword: Optional[str] = Field(
        None, min_length=1, max_length=200, description="Từ khóa tìm trên ô tìm kiếm Facebook."
    )
    page: Optional[str] = Field(
        None,
        max_length=500,
        description="Link/tên Page cần quét feed. Gửi field này thay cho 'keyword'.",
    )
    max_posts: int = Field(0, ge=0, le=5000)
    search_all: bool = Field(False)
    keyword_match: Literal["strict", "normal", "loose", "off"] = Field(
        "strict", description="Mức khớp từ khóa trong tiêu đề/nội dung (xem POST /api/scan)."
    )
    sort: Literal["recent", "relevance"] = Field("recent", description="recent = bài mới nhất trước.")
    engine: Literal["auto", "graphql", "browser"] = Field(
        "auto", description="auto = GraphQL rồi tự chuyển sang browser khi bị chặn; browser = luôn đọc trang."
    )
    download_media: bool = Field(True)
    resume: bool = Field(
        True,
        description="Chỉ với 'page': quét tiếp từ mốc lần quét trước (bỏ qua bài đã quét). "
        "false = quét lại toàn bộ khoảng đã chọn.",
    )
    exclusions: list[str] = Field(default_factory=list, max_length=1000)
    excluded_terms: list[str] = Field(default_factory=list, max_length=50)
    exclude_sources: list[str] = Field(default_factory=list, max_length=200)
    created_by: str = Field("web-ui", max_length=60)


# ------------------------------------------------------------------ helpers
def _today_vn() -> _dt.date:
    """Ngày hôm nay theo giờ Việt Nam (bài đăng lúc 2h sáng vẫn tính là hôm nay)."""
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def _date_filters(rng: dtrange.DateRange, lookback: Optional[int], *lists: list[str]) -> dict:
    return {
        "rng": rng,
        "max_posts": None,
        "lookback": lookback or None,
        "exclusions": lists[0] if lists else [],
        "excluded_terms": lists[1] if len(lists) > 1 else [],
        "exclude_sources": lists[2] if len(lists) > 2 else [],
    }


def _scope_key(keyword_id: Optional[int], group_id: Optional[int], search_all: bool) -> str:
    """Định danh 'cùng một phạm vi' để biết năm/ngày nào đã quét xong."""
    if keyword_id:
        return f"kw:{keyword_id}:{'all' if search_all else 'group'}"
    return f"group:{group_id or 0}"


# ------------------------------------------------------------------ helpers
def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def _one(sql: str, params: tuple = ()) -> Optional[dict]:
    with db.get_conn() as conn:
        return conn.execute(sql, params).fetchone()


def _media_urls(post_id: int) -> list[dict]:
    rows = _rows(
        """
        SELECT id, kind, position, file_path, thumb_url, width, height, source_url
        FROM scan_post_media WHERE post_id=%s ORDER BY position
        """,
        (post_id,),
    )
    for r in rows:
        if r["file_path"]:
            r["url"] = f"/media/{r['file_path']}"
        else:
            r["url"] = r["source_url"]
        r.pop("file_path", None)
    return rows


def _shape_post(p: dict, include_media: bool = True) -> dict:
    p = dict(p)
    p["group_name"] = p.pop("group_name", None)
    p["summary"] = (p.get("title") or (p.get("content") or ""))[:200]
    if include_media:
        p["media"] = _media_urls(p["id"])
        p["cover_url"] = next((m["url"] for m in p["media"] if m["url"]), None)
    return p


# ------------------------------------------------------------------- routes
@app.get("/api/health", tags=["system"])
def health() -> dict:
    try:
        row = _one("SELECT version() AS v, now() AS t")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"DB không sẵn sàng: {exc}") from exc
    return {"ok": True, "version": __version__, "db": row["v"], "server_time": row["t"]}


@app.get("/api/groups", tags=["groups"])
def list_groups() -> list[dict]:
    return _rows(
        """
        SELECT g.id, g.fb_group_id, g.slug, g.name, g.privacy, g.member_count_txt,
               g.group_url, g.is_active, g.last_scanned_at,
               (SELECT count(*) FROM scan_posts p WHERE p.group_id=g.id)::int AS post_count,
               (SELECT count(*) FROM scan_posts p WHERE p.group_id=g.id AND NOT p.is_read)::int AS unread_count,
               (SELECT max(posted_at) FROM scan_posts p WHERE p.group_id=g.id) AS latest_post_at
        FROM scan_groups g
        ORDER BY g.is_active DESC, g.name
        """
    )


@app.get("/api/groups/{group_id}", tags=["groups"])
def get_group(group_id: int) -> dict:
    g = _one("SELECT * FROM scan_groups WHERE id=%s", (group_id,))
    if not g:
        raise HTTPException(404, "Không thấy group")
    g["post_count"] = _one(
        "SELECT count(*)::int AS c FROM scan_posts WHERE group_id=%s", (group_id,)
    )["c"]
    return g


@app.get("/api/posts", tags=["posts"])
def list_posts(
    group_id: Optional[int] = None,
    q: Optional[str] = Query(None, description="tìm trong tiêu đề + nội dung"),
    author: Optional[str] = Query(None, description="tên tác giả (ILIKE)"),
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    has_media: Optional[bool] = None,
    is_read: Optional[bool] = Query(None, description="lọc bài đã đọc / chưa đọc"),
    keyword_id: Optional[int] = Query(None, description="bài tìm thấy từ khóa này"),
    source_type: Optional[Literal["group", "page", "user"]] = Query(
        None, description="nguồn bài: group / page / user"
    ),
    sort: Literal["posted_at", "like_count", "comment_count", "scraped_at"] = "posted_at",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict:
    where, params = [], []
    if group_id:
        where.append("p.group_id = %s")
        params.append(group_id)
    if q:
        where.append("(p.content ILIKE %s OR p.title ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if author:
        where.append("p.author_name ILIKE %s")
        params.append(f"%{author}%")
    if date_from:
        where.append("p.posted_at >= %s")
        params.append(date_from)
    if date_to:
        where.append("p.posted_at <= %s")
        params.append(date_to)
    if has_media is not None:
        where.append("p.has_media = %s")
        params.append(has_media)
    if is_read is not None:
        where.append("p.is_read = %s")
        params.append(is_read)
    if keyword_id:
        where.append("p.keyword_id = %s")
        params.append(keyword_id)
    if source_type:
        where.append("p.source_type = %s")
        params.append(source_type)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT count(*)::int AS c FROM scan_posts p {clause}", tuple(params))["c"]

    rows = _rows(
        f"""
        SELECT p.id, p.fb_post_id, p.group_id, g.name AS group_name, p.author_fb_id,
               p.author_name, p.title, p.content, p.permalink, p.posted_at,
               p.like_count, p.comment_count, p.share_count, p.has_media, p.scraped_at,
               p.is_read, p.note, p.updated_by, p.keyword_id, k.term AS keyword,
               p.source_type, p.source_fb_id, p.source_name, p.source_url
        FROM scan_posts p
        LEFT JOIN scan_groups g ON g.id = p.group_id
        LEFT JOIN scan_keywords k ON k.id = p.keyword_id
        {clause}
        ORDER BY p.{sort} {order} NULLS LAST, p.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params) + (size, (page - 1) * size),
    )
    return {
        "items": [_shape_post(r) for r in rows],
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size,
    }


@app.get("/api/posts/{post_id}", tags=["posts"])
def get_post(post_id: int) -> dict:
    row = _one(
        """
        SELECT p.*, g.name AS group_name, g.group_url, k.term AS keyword
        FROM scan_posts p
        LEFT JOIN scan_groups g ON g.id=p.group_id
        LEFT JOIN scan_keywords k ON k.id=p.keyword_id
        WHERE p.id=%s
        """,
        (post_id,),
    )
    if not row:
        raise HTTPException(404, "Không thấy bài viết")
    row.pop("raw", None)
    return _shape_post(row)


# --------------------------------------------------- posts: thao tác (UI)
def _delete_post_media(post_id: int) -> int:
    """Xoá file ảnh vật lý của bài, trả về số file đã xoá."""
    base = config.MEDIA_DIR.resolve()
    deleted = 0
    for r in _rows("SELECT file_path FROM scan_post_media WHERE post_id=%s", (post_id,)):
        fp = r["file_path"]
        if not fp:
            continue
        target = (base / fp).resolve()
        if str(target).startswith(str(base)) and target.is_file():
            try:
                target.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


def _delete_posts(post_ids: list[int]) -> dict:
    files = 0
    for pid in post_ids:
        files += _delete_post_media(pid)
    with db.get_conn() as conn:
        n = conn.execute(
            "DELETE FROM scan_posts WHERE id = ANY(%s)", (post_ids,)
        ).rowcount
    return {"deleted": n, "files_deleted": files}


@app.patch("/api/posts/{post_id}", tags=["posts"])
def update_post(post_id: int, body: PostUpdateIn) -> dict:
    """Đánh dấu đã đọc / ghi chú cho 1 bài."""
    sets, params = [], []
    if body.is_read is not None:
        sets.append("is_read = %s")
        params.append(body.is_read)
    if body.note is not None:
        sets.append("note = %s")
        params.append(body.note)
    if not sets:
        raise HTTPException(422, "Không có field nào để cập nhật")
    sets.append("updated_by = %s")
    params.append(body.updated_by)
    with db.get_conn() as conn:
        row = conn.execute(
            f"UPDATE scan_posts SET {', '.join(sets)} WHERE id=%s RETURNING id, is_read, note, updated_by",
            tuple(params) + (post_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Không thấy bài viết")
    return row


@app.post("/api/posts/bulk-read", tags=["posts"])
def bulk_mark_read(body: IdsIn) -> dict:
    """Đánh dấu nhiều bài đã đọc."""
    with db.get_conn() as conn:
        n = conn.execute(
            "UPDATE scan_posts SET is_read=true, updated_by='ui' WHERE id = ANY(%s)",
            (body.ids,),
        ).rowcount
    return {"updated": n}


@app.delete("/api/posts/{post_id}", tags=["posts"])
def delete_post(post_id: int) -> dict:
    """Xoá 1 bài (xem trước bằng `GET /api/posts/{id}/delete-preview`)."""
    with db.get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM scan_posts WHERE id=%s", (post_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Không thấy bài viết")
    return {"id": post_id, **_delete_posts([post_id])}


@app.delete("/api/posts", tags=["posts"])
def delete_posts_bulk(
    body: IdsIn,
    confirm: bool = Query(False, description="Bắt buộc true để xoá nhiều bài"),
) -> dict:
    """Xoá nhiều bài + ảnh đã tải của chúng (cần `?confirm=true`)."""
    if not confirm:
        raise HTTPException(422, "Cần ?confirm=true để xoá nhiều bài")
    return _delete_posts(body.ids)


@app.get("/api/posts/{post_id}/delete-preview", tags=["posts"])
def post_delete_preview(post_id: int) -> dict:
    """Cho UI hỏi trước khi xoá: bài tồn tại không, sẽ xoá bao nhiêu file ảnh."""
    post = _one(
        "SELECT id, title, author_name, posted_at, group_id FROM scan_posts WHERE id=%s",
        (post_id,),
    )
    if not post:
        raise HTTPException(404, "Không thấy bài viết")
    files = _one(
        "SELECT count(*)::int AS c FROM scan_post_media WHERE post_id=%s AND file_path IS NOT NULL",
        (post_id,),
    )["c"]
    return {**post, "media_files": files}


# --------------------------------------------------- groups: thao tác (UI)
@app.patch("/api/groups/{group_id}", tags=["groups"])
def update_group(group_id: int, body: GroupUpdateIn) -> dict:
    """Đổi tên/bật tắt group."""
    sets, params = [], []
    for field in ("name", "slug", "is_active", "privacy", "member_count_txt", "group_url"):
        value = getattr(body, field)
        if value is not None:
            sets.append(f"{field} = %s")
            params.append(value)
    if not sets:
        raise HTTPException(422, "Không có field nào để cập nhật")
    with db.get_conn() as conn:
        row = conn.execute(
            f"UPDATE scan_groups SET {', '.join(sets)} WHERE id=%s RETURNING *",
            tuple(params) + (group_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Không thấy group")
    return row


@app.get("/api/stats", tags=["stats"])
def stats(
    group_id: Optional[int] = None,
    days: int = Query(7, ge=1, le=365),
) -> dict:
    where = "WHERE posted_at >= now() - make_interval(days => %s)"
    params: list = [days]
    if group_id:
        where += " AND group_id = %s"
        params.append(group_id)

    total = _one(f"SELECT count(*)::int AS c FROM scan_posts {where}", tuple(params))["c"]
    by_day = _rows(
        f"""
        SELECT to_char(date_trunc('day', posted_at), 'YYYY-MM-DD') AS day,
               count(*)::int AS posts, sum(like_count)::int AS likes,
               sum(comment_count)::int AS comments
        FROM scan_posts {where}
        GROUP BY 1 ORDER BY 1
        """,
        tuple(params),
    )
    top_authors = _rows(
        f"""
        SELECT author_name, count(*)::int AS posts, sum(like_count)::int AS likes
        FROM scan_posts {where}
        GROUP BY author_name ORDER BY posts DESC NULLS LAST LIMIT 10
        """,
        tuple(params),
    )
    top_posts = _rows(
        f"""
        SELECT id, title, author_name, like_count, comment_count, posted_at, group_id
        FROM scan_posts {where}
        ORDER BY like_count DESC NULLS LAST LIMIT 10
        """,
        tuple(params),
    )
    return {
        "days": days,
        "total": total,
        "by_day": by_day,
        "top_authors": top_authors,
        "top_posts": top_posts,
    }


@app.get("/api/jobs", tags=["system"])
def list_jobs(
    status: Optional[Literal["queued", "running", "done", "error"]] = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    if status:
        return _rows(
            """
            SELECT j.*, g.name AS group_name FROM scan_jobs j
            LEFT JOIN scan_groups g ON g.id=j.group_id
            WHERE j.status=%s ORDER BY j.id DESC LIMIT %s
            """,
            (status, limit),
        )
    return _rows(
        """
        SELECT j.*, g.name AS group_name FROM scan_jobs j
        LEFT JOIN scan_groups g ON g.id=j.group_id
        ORDER BY j.id DESC LIMIT %s
        """,
        (limit,),
    )


# --------------------------------------------------------- scan requests
@app.get("/api/scan", tags=["scan"])
def list_scan_requests(
    status: Optional[Literal["queued", "running", "done", "error", "canceled"]] = None,
    group_id: Optional[int] = None,
    keyword_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Hàng đợi yêu cầu quét (mới nhất trước)."""
    with db.get_conn() as conn:
        return db.list_requests(
            conn, status=status, group_id=group_id, keyword_id=keyword_id, limit=limit
        )


# ------------------------------------------------------------- keywords
@app.get("/api/keywords", tags=["keywords"])
def list_keywords() -> list[dict]:
    """Danh sách từ khóa đã dùng để quét ô tìm kiếm Facebook + số bài mỗi từ khóa."""
    with db.get_conn() as conn:
        return db.list_keywords(conn)


@app.delete("/api/keywords/{keyword_id}", tags=["keywords"])
def delete_keyword(keyword_id: int) -> dict:
    """Xoá từ khóa. Bài đã quét vẫn giữ trong `scan_posts` (chỉ mất liên kết từ khóa)."""
    with db.get_conn() as conn:
        n = conn.execute("DELETE FROM scan_keywords WHERE id=%s", (keyword_id,)).rowcount
    if not n:
        raise HTTPException(404, "Không thấy từ khóa")
    return {"id": keyword_id, "deleted": True}


@app.get("/api/scan/status", tags=["scan"])
def scan_status() -> dict:
    """Đếm yêu cầu đang chờ/đang chạy + lần quét gần nhất."""
    with db.get_conn() as conn:
        counts = db.pending_request_count(conn)
        last = conn.execute(
            "SELECT id, status, started_at, finished_at, error FROM scan_requests "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {**counts, "last_request": last}


@app.get("/api/scan/{request_id}", tags=["scan"])
def get_scan_request(request_id: int) -> dict:
    with db.get_conn() as conn:
        row = db.get_request(conn, request_id)
    if not row:
        raise HTTPException(404, "Không thấy yêu cầu quét")
    return row


@app.post("/api/scan", status_code=201, tags=["scan"])
def create_scan_request(body: ScanRequestIn) -> dict:
    """Đặt yêu cầu quét.

    - **Theo group**: gửi `group_id` hoặc `group` (id/slug/link Facebook).
    - **Theo từ khóa** (ô tìm kiếm Facebook): gửi `keyword`.

    Yêu cầu được ghi vào `scan_requests` (status=queued). Worker thực thi khi chạy:

        python scripts/run_scan.py --pending

    Nếu worker đang chạy loop (`--pending --watch 60`), nó tự nhận trong vòng ~60s.
    """
    if body.keyword and (body.group_id or body.group):
        raise HTTPException(422, "Chỉ gửi group_id/group HOẶC keyword, không gửi cả hai")
    if not body.keyword and not body.group_id and not body.group:
        raise HTTPException(422, "Cần group_id, group (id/slug/link Facebook) hoặc keyword")

    try:
        rng = body.to_range()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if body.keyword:
        try:
            with db.get_conn() as conn:
                kw = db.upsert_keyword(conn, body.keyword)
                req = db.create_request(
                    conn,
                    None,
                    keyword_id=kw["id"],
                    max_posts=body.max_posts,
                    days=body.days,
                    date_from=rng.start,
                    date_to=rng.end,
                    reset_cursor=False,
                    download_media=body.download_media,
                    search_all=body.search_all,
                    keyword_match=body.keyword_match,
                    sort=body.sort,
                    exclusions=body.exclusions,
                    excluded_terms=body.excluded_terms,
                    exclude_sources=body.exclude_sources,
                    scope_key=_scope_key(kw["id"], None, body.search_all),
                    requested_by=body.requested_by,
                    note=body.note,
                )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            **req,
            "keyword": kw["term"],
            "group_name": None,
            "range": rng.describe(),
            "hint": "Chạy: python scripts/run_scan.py --pending  (hoặc để worker --watch tự nhận)",
        }

    with db.get_conn() as conn:
        if body.group_id:
            grp = db.get_group(conn, body.group_id)
        else:
            ref = body.group.strip().strip("/")
            if "/groups/" in ref:
                ref = ref.split("/groups/")[-1].split("?")[0].strip("/")
            grp = conn.execute(
                "SELECT * FROM scan_groups WHERE fb_group_id=%s OR slug=%s LIMIT 1",
                (ref, ref),
            ).fetchone()
            if not grp and not ref.isdigit():
                raise HTTPException(
                    404,
                    f"Group '{ref}' chưa có trong DB. Chạy discover trước, "
                    "hoặc POST với group_id sau khi thêm group.",
                )
            if not grp:
                gid = db.upsert_group(conn, ref, slug=None, name=None)
                grp = db.get_group(conn, gid)

        req = db.create_request(
            conn,
            grp["id"],
            max_posts=body.max_posts,
            days=body.days,
            date_from=rng.start,
            date_to=rng.end,
            reset_cursor=body.reset_cursor,
            download_media=body.download_media,
            exclusions=body.exclusions,
            excluded_terms=body.excluded_terms,
            exclude_sources=body.exclude_sources,
            scope_key=_scope_key(None, grp["id"], False),
            requested_by=body.requested_by,
            note=body.note,
        )
    return {
        **req,
        "group_name": grp.get("name"),
        "fb_group_id": grp.get("fb_group_id"),
        "range": rng.describe(),
        "hint": (
            "Chạy: python scripts/run_scan.py --pending  (hoặc để worker --watch tự nhận). "
            "Lưu ý: quét feed của group không đi lùi tới bài cũ, chỉ quét được bài "
            "Facebook còn trả về — muốn đối chiếu hãy dùng /api/search/preview + /api/search/review."
        ),
    }


# ------------------------------------------------------------- search tools
def _need_ctx():
    """Phiên GraphQL. Trả None khi cookies hỏng — search_scan sẽ tự chuyển sang browser."""
    from fbscan import context as fbctx

    try:
        return fbctx.make_context(quiet=True)
    except fbctx.LoginRequired:
        return None


@app.post("/api/search/preview", tags=["search"])
def search_preview(body: PreviewIn) -> dict:
    """**Kiểm tra trước** (không ghi DB): Facebook trả về gì với từ khóa + khoảng thời gian này.

    Dùng để thấy ngay: bao nhiêu bài đúng khoảng thời gian, bao nhiêu đã có trong DB,
    bao nhiêu bị bỏ (không khớp từ khóa / không phải bài group / bị loại trừ), kèm tối đa
    `sample` bài mẫu có link Facebook để mở đối chiếu.
    """
    from fbscan import search_scan

    try:
        rng = body.to_range()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    page = (body.page or "").strip() or None
    term = (body.keyword or "").strip()
    if not page and not term:
        raise HTTPException(422, "Cần gửi 'keyword' (từ khóa) hoặc 'page' (link/tên Page)")
    ctx = None if page else _need_ctx()
    return search_scan.preview_search(
        ctx,
        term,
        sample=body.sample,
        rng=rng,
        lookback=body.lookback,
        max_posts=body.max_posts,
        search_all=True if page else body.search_all,
        keyword_match="off" if page else body.keyword_match,
        sort=body.sort,
        engine="browser" if page else body.engine,
        page=page,
        resume=body.resume,
        exclusions=body.exclusions,
        excluded_terms=body.excluded_terms,
        exclude_sources=body.exclude_sources,
    )


def _run_review_job(term: str, request_id: Optional[int], created_by: str, filters: dict) -> None:
    """Chạy review trong background (không chặn request)."""
    from fbscan import context as fbctx, search_scan

    try:
        try:
            ctx = fbctx.make_context(quiet=True)
        except fbctx.LoginRequired:
            ctx = None
        search_scan.review_search(
            ctx, term, request_id=request_id, created_by=created_by,
            download_media=filters.pop("download_media", True), **filters,
        )
    except Exception as exc:  # noqa: BLE001 - chỉ log, kết quả xem ở /api/search/reviews
        print(f"X review '{term}': {type(exc).__name__}: {exc}", flush=True)


@app.post("/api/search/review", tags=["search"])
def search_review(body: ReviewIn, background: BackgroundTasks) -> dict:
    """**Review thật**: chạy tìm kiếm + lưu bài mới (như quét) + ghi `scan_reviews`.

    Trả `review_id` ngay (chạy nền); xem kết quả bằng `GET /api/search/reviews/{review_id}`
    — danh sách bài mới lưu, bài đã có, bài bị bỏ kèm lý do, mỗi bài có link Facebook.
    """
    from fbscan import search_scan

    try:
        rng = body.to_range()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    page = (body.page or "").strip() or None
    keyword = (body.keyword or "").strip()
    if not page and not keyword:
        raise HTTPException(422, "Cần gửi 'keyword' (từ khóa) hoặc 'page' (link/tên Page)")

    kw_id = None
    term = page or keyword
    if not page:
        with db.get_conn() as conn:
            kw = db.upsert_keyword(conn, keyword)
        kw_id, term = kw["id"], kw["term"]

    filters = {
        "rng": rng,
        "lookback": body.lookback,
        "max_posts": body.max_posts,
        "search_all": True if page else body.search_all,
        "keyword_match": "off" if page else body.keyword_match,
        "sort": body.sort,
        "engine": "browser" if page else body.engine,
        "page": page,
        "resume": body.resume,
        "exclusions": body.exclusions,
        "excluded_terms": body.excluded_terms,
        "exclude_sources": body.exclude_sources,
        "keyword_id": kw_id,
        "download_media": body.download_media,
    }
    background.add_task(_run_review_job, term, None, body.created_by, filters)
    return {
        "status": "started",
        "keyword_id": kw_id,
        "keyword": term,
        "page": page,
        "range": rng.describe(),
        "skipped_reason": search_scan.SKIP_REASONS,
        "hint": "Đang chạy nền. Xem kết quả ở GET /api/search/reviews?keyword_id=... sau ít phút.",
    }


@app.get("/api/search/reviews", tags=["search"])
def list_reviews(
    keyword_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Lịch sử review (mới nhất trước), kèm số bài mới / đã có / bỏ qua."""
    with db.get_conn() as conn:
        rows = db.list_reviews(conn, keyword_id=keyword_id, limit=limit)
    out = []
    for r in rows:
        out.append({
            **{k: v for k, v in r.items() if k not in ("new_ids", "existing_ids", "skipped")},
            "new_count": len(r.get("new_ids") or []),
            "existing_count": len(r.get("existing_ids") or []),
            "skipped_count": len(r.get("skipped") or []),
            "range": dtrange.DateRange(r.get("date_from"), r.get("date_to")).describe(),
        })
    return out


@app.get("/api/search/reviews/{review_id}", tags=["search"])
def get_review(
    review_id: int,
    with_posts: bool = Query(True, description="kèm nội dung/link của bài mới + đã có"),
    sample: int = Query(100, ge=1, le=500),
) -> dict:
    """Chi tiết 1 lần review: bài mới lưu, bài đã có, bài bị bỏ kèm lý do."""
    from fbscan import search_scan

    with db.get_conn() as conn:
        rv = db.get_review(conn, review_id)
        if not rv:
            raise HTTPException(404, "Không thấy review")
        rng = dtrange.DateRange(rv.get("date_from"), rv.get("date_to"))
        note = None
        if rv.get("in_range") == 0 and rv.get("found") and rng.start:
            note = (
                f"Facebook không trả bài nào trong khoảng {rng.describe()} "
                f"(đã đọc {rv['found']} bài, đều ngoài khoảng)."
            )
        out = {
            **{k: v for k, v in rv.items() if k not in ("skipped",)},
            "range": rng.describe(),
            "note": note,
            "new_count": len(rv.get("new_ids") or []),
            "existing_count": len(rv.get("existing_ids") or []),
            "skipped_count": len(rv.get("skipped") or []),
            "skipped": (rv.get("skipped") or [])[:sample],
        }
        if with_posts:
            ids = list(rv.get("new_ids") or [])[:sample]
            if ids:
                out["new_posts"] = _rows(
                    """
                    SELECT p.id, p.fb_post_id, p.title, p.content, p.permalink, p.posted_at,
                           p.author_name, p.source_type, p.source_name, p.group_id,
                           g.name AS group_name,
                           (SELECT count(*) FROM scan_post_media m WHERE m.post_id=p.id)::int AS media_count
                    FROM scan_posts p LEFT JOIN scan_groups g ON g.id=p.group_id
                    WHERE p.id = ANY(%s) ORDER BY p.posted_at DESC NULLS LAST
                    """,
                    (ids,),
                )
    out["reasons"] = search_scan.SKIP_REASONS
    return out


@app.delete("/api/scan/{request_id}", tags=["scan"])
def cancel_scan_request(request_id: int) -> dict:
    """Huỷ yêu cầu còn ở trạng thái queued."""
    with db.get_conn() as conn:
        row = db.cancel_request(conn, request_id)
        if not row:
            current = db.get_request(conn, request_id)
    if not row:
        if not current:
            raise HTTPException(404, "Không thấy yêu cầu quét")
        raise HTTPException(409, f"Chỉ huỷ được yêu cầu 'queued' (hiện tại: {current['status']})")
    return row


@app.get("/media/{file_path:path}", tags=["media"], include_in_schema=False)
def serve_media(file_path: str) -> FileResponse:
    base = config.MEDIA_DIR.resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404, "Không thấy file")
    mt = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=mt, headers={"cache-control": "public, max-age=86400"})


# ------------------------------------------------------------ page cursors
@app.get("/api/pages/cursors", tags=["search"])
def list_page_cursors(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    """Mốc quét của các Page: bài mới nhất đã quét + lần quét gần nhất."""
    from fbscan import search_scan

    rows = _rows(
        """
        SELECT source_key, page_url, page_name, last_posted_at, last_fb_post_id,
               last_scan_at, last_found, last_new, note
        FROM scan_page_cursors ORDER BY last_scan_at DESC LIMIT %s
        """,
        (limit,),
    )
    for r in rows:
        r["last_posted_text"] = search_scan._fmt_vn(r.get("last_posted_at"))
        r["last_scan_text"] = search_scan._fmt_vn(r.get("last_scan_at"))
    return rows


@app.delete("/api/pages/cursors/{source_key}", tags=["search"])
def reset_page_cursor(source_key: str) -> dict:
    """Xoá mốc quét của 1 Page — lần quét sau sẽ quét lại toàn bộ khoảng đã chọn."""
    with db.get_conn() as conn:
        ok = db.delete_page_cursor(conn, source_key.strip().lower())
    if not ok:
        raise HTTPException(404, f"Không thấy mốc quét cho Page '{source_key}'")
    return {"deleted": True, "source_key": source_key.strip().lower()}
