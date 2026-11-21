"""Tìm kiếm trên Facebook và **đối chiếu kết quả** với dữ liệu đã quét.

Cùng một lõi (`_crawl`) dùng cho 3 việc:

1. **Quét theo từ khóa** (worker `run_scan.py --pending` / API `POST /api/scan`) —
   đọc ô tìm kiếm Facebook, lưu bài mới vào `scan_posts`; có thêm lọc theo năm/ngày.
2. **Kiểm tra trước** (`preview_search`): chỉ đếm + liệt kê thử những gì Facebook trả
   về (đúng khoảng thời gian? đã có trong DB? bị loại trừ?), KHÔNG ghi DB, KHÔNG tải ảnh.
3. **Review / verify** (`review_search`): chạy như quét, nhưng ghi một dòng `scan_reviews`
   lưu danh sách id bài mới / đã có / bị bỏ qua kèm lý do để đối chiếu với Facebook.

Quy ước quan trọng:

- Kết quả tìm kiếm Facebook xếp **mới trước**, nhưng bài ngoài khoảng năm/ngày vẫn chen
  lẫn nên ta **không dừng** ở bài ngoài khoảng đầu tiên: lọc từng bài tới khi hết trang
  hoặc đọc qua `lookback` bài.
- "Bỏ qua bài đã có trong DB" (`skip_existing`): bài đã có không tải lại chi tiết/ảnh.
- Loại trừ: danh sách fb_post_id, cụm từ trong nội dung, nguồn (Page/trang cá nhân).
- Chỉ lưu bài có nội dung chứa từ khóa (so khớp bỏ dấu, không phân biệt hoa thường) —
  kết quả của Facebook hay lẫn bài không liên quan.
"""

from __future__ import annotations

import json
import random
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import facebook_gql_scraper as fgs

from . import browser_search, config, db, dtrange, media, normalize
from .keywords import _needs_detail, _safe_attr, _source_from_url, _source_of
from facebook_gql_scraper.facebookGraphql import (
    SearchCometResultsPaginatedResultsQueryGraphQL as _SerpQuery,
)

# Giới hạn mặc định số bài đọc qua kết quả tìm kiếm (kể cả bài ngoài khoảng) để không
# quét vô tận khi không lọc thời gian.
DEFAULT_LOOKBACK = 1000
PAGE_BATCH = 25  # in tiến độ + commit mỗi chừng này bài

# Bao nhiêu bài liên tiếp bị bỏ vì lý do "mềm" (đã có / ngoài khoảng / loại trừ) thì dừng.
# Bài bị bỏ vì "không liên quan" (not_match / not_group) KHÔNG tính, vì Facebook hay trả
# lan man — dừng theo loại này sẽ bỏ lỡ kết quả đúng ở các trang sau.
SOFT_STOP_AFTER = 500
SOFT_REASONS = {"already_in_db", "out_of_range", "excluded_id", "excluded_term", "excluded_source"}

# Lý do một bài không được lưu (khoá trong scan_reviews.skipped[].reason)
SKIP_REASONS = db.REVIEW_SKIP_REASONS


# --------------------------------------------------------------- so khớp từ khóa
def strip_accents(text: str) -> str:
    """Bỏ dấu tiếng Việt + lowercase: 'Tuyển Dụng' -> 'tuyen dung'."""
    text = (text or "").replace("đ", "d").replace("Đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()


def _tokens(text: Optional[str]) -> list[str]:
    return strip_accents(text).replace("\n", " ").split()


# Từ dừng của tiếng Việt: không tính khi kiểm tra "có đủ ý từ khóa không"
# (chúng xuất hiện ở mọi bài nên trùng 1 từ như "cần"/"và" không nói lên điều gì).
VN_STOPWORDS = {
    "va", "cua", "cho", "voi", "thi", "la", "co", "cac", "nhung", "mot", "nhu",
    "de", "tai", "trong", "khi", "den", "tu", "hay", "hoac", "ma", "nay", "do",
    "can", "dang", "se", "da", "bi", "boi", "ve", "ra", "vao", "len", "xuong",
    "rat", "hon", "mot", "cung", "chi", "ma", "nhe", "nha", "roi", "gi",
}

# Mức khớp một bài: exact (đúng cụm) / slug (đủ mọi từ khoá) / loose (đủ các từ CHÍNH,
# chỉ thiếu từ phụ như "tuyển") / weak (chỉ trùng từ phụ, thiếu từ chính) / none.
MATCH_LEVELS = ("exact", "slug", "loose", "weak", "none")
MATCH_LEVEL_TEXT = {
    "exact": "Đúng cụm từ khóa",
    "slug": "Đủ mọi từ của từ khóa",
    "loose": "Chứa một cụm của từ khóa",
    "weak": "Chỉ trùng một từ rời",
    "none": "Không chứa từ khóa",
}


def _wanted(term: str) -> tuple[list[str], list[str]]:
    """(từ chính, từ phụ). Từ chính = không phải từ dừng; nếu toàn từ dừng thì lấy hết."""
    tokens = [t for t in _tokens(term) if len(t) >= 2]
    core = [t for t in tokens if t not in VN_STOPWORDS]
    return (core or tokens), tokens


def _phrase_parts(needle: str) -> list[str]:
    """Các cụm liền nhau của từ khóa: 'tuyen dung ke toan' -> 'tuyen dung', 'dung ke toan'...

    Dùng từ 2 từ trở lên: cụm trùng với một phần từ khóa (vd tiêu đề "TUYỂN DỤNG KẾ TOÁN
    - BÁN HÀNG" chỉ chứa cụm đầu) đáng tin hơn hẳn một từ rời ("tuyển" xuất hiện ở mọi
    tin tuyển dụng).
    """
    words = needle.split()
    return [" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 2, len(words) + 1)]


def score_match(text: Optional[str], term: str) -> tuple[str, list[str]]:
    """Chấm bài có khớp từ khóa không: trả (mức, các từ CHÍNH còn thiếu)."""
    hay = strip_accents(text).replace("\n", " ")
    needle = " ".join(_tokens(term))
    if not needle:
        return "loose", []
    if needle in hay:
        return "exact", []
    hay_tokens = set(hay.split())
    core, tokens = _wanted(term)
    missing = [t for t in core if t not in hay_tokens]
    parts = _phrase_parts(needle)
    # Cụm "có nghĩa" = chứa ít nhất 1 từ chính (cụm chỉ gồm từ dừng như "dụng kế" không tính).
    if any(p in hay and any(w in core for w in p.split()) for p in parts):
        return "loose", missing

    if any(t in hay_tokens for t in core):
        return "weak", missing
    # Chỉ trùng từ phụ ("dụng cụ" với từ khóa "tuyển dụng kế toán"): không tính.
    return "none", missing


def accepted_levels(mode: str) -> set[str]:
    """Các mức khớp được coi là 'bài đúng từ khóa' theo chế độ lọc.

    strict : đúng cụm (exact), đủ mọi từ (slug), hoặc chứa NGUYÊN một cụm của từ khóa
             (loose, vd tiêu đề "TUYỂN DỤNG KẾ TOÁN - BÁN HÀNG")
    normal : như strict + bài chỉ trùng một từ có nghĩa (weak, vd "Kế toán cần người")
    loose  : nhận mọi bài có ít nhất 1 từ khớp
    off    : nhận tất cả (không kiểm tra từ khóa)
    """
    if mode in ("normal", "loose"):
        return {"exact", "slug", "loose", "weak"}
    return {"exact", "slug", "loose"}


def matches(hit, plan) -> bool:
    """Bài có liên quan tới từ khóa theo chế độ `keyword_match` không.

    Xét cả nội dung VÀ tên nguồn (group/Page): kết quả tìm kiếm Facebook trả về bài từ
    nhóm/trang có tên chứa từ khóa (vd tìm "thái bình" -> bài trong group "XE TẢI GHÉP
    HÀNG THÁI BÌNH") dù nội dung bài không lặp lại từ khóa. Chỉ khi nội dung lẫn tên
    nguồn đều không chứa từ khóa mới coi là không liên quan.
    """
    if plan.keyword_match == "off" or not plan.term:
        return True
    level, _ = score_match(hit.text or "", plan.term)
    if level not in accepted_levels(plan.keyword_match) and hit.source_name:
        src_level, _ = score_match(hit.source_name, plan.term)
        if src_level in accepted_levels(plan.keyword_match):
            level = src_level if level == "none" else level
            hit.matched_by = "source"
    hit.level = level
    return level in accepted_levels(plan.keyword_match)


# ------------------------------------------------------------------- dataclass
@dataclass
class SearchHit:
    """Một bài Facebook trả về trong lượt tìm/lọc."""

    fb_post_id: str
    posted_at: Optional[datetime]
    url: Optional[str]
    text: Optional[str]
    author_name: Optional[str]
    source_type: str
    source_fb_id: Optional[str]
    source_name: Optional[str]
    in_range: bool
    existing_id: Optional[int] = None
    level: str = "loose"
    matched_by: str = "content"  # content | source (khớp qua tên group/Page)
    group_url: Optional[str] = None
    post: Optional[fgs.Post] = None  # giữ để lưu chi tiết
    browser: Optional["browser_search.BrowserHit"] = None  # bài đọc từ trang (không phải GraphQL)

    def brief(self) -> dict:
        return {
            "fb_post_id": self.fb_post_id,
            "url": self.url,
            "permalink": self.url,
            "text": self.text,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "author_name": self.author_name,
            "source_type": self.source_type,
            "source_fb_id": self.source_fb_id,
            "source_name": self.source_name,
            "in_range": self.in_range,
            "existing_id": self.existing_id,
            "match": self.level,
            "snippet": (self.text or "")[:300],
        }


@dataclass
class Outcome:
    """Kết quả xử lý 1 bài: reason=None nghĩa là đã lưu mới (post_id có giá trị)."""

    hit: SearchHit
    reason: Optional[str] = None
    post_id: Optional[int] = None


@dataclass
class CrawlResult:
    term: str
    found: int = 0       # số bài đã đọc từ kết quả tìm kiếm
    in_range: int = 0    # trong đó: đúng khoảng thời gian
    new: int = 0         # bài mới lưu (hoặc "sẽ lưu" khi chỉ kiểm tra)
    existing: int = 0    # bài đã có trong DB
    skipped: int = 0     # bỏ qua vì lý do khác
    failed: int = 0      # lỗi
    past: int = 0        # số bài cũ hơn mốc đầu khoảng đã gặp (để biết khi nào dừng)
    oldest_seen: Optional[datetime] = None
    times: list = field(default_factory=list)  # ngày đăng các bài đã đọc (để đo "lan man")
    stopped_reason: str = ""
    errors: list[str] = field(default_factory=list)
    engine: str = "graphql"  # graphql | browser (browser = đọc trang tìm kiếm, không qua GraphQL)
    rejected: list = field(default_factory=list)  # lỗi gây dừng hẳn (Facebook từ chối / không mở được)
    page: Optional[str] = None  # Page đang quét (chế độ quét feed Page, không phải ô tìm kiếm)
    page_name: str = ""         # tên Page đọc được từ Facebook
    since: Optional[datetime] = None        # mốc quét lần trước đang dùng (chỉ lấy bài mới hơn)
    newest_posted_at: Optional[datetime] = None  # bài mới nhất đã đọc (để cập nhật mốc)
    newest_fb_post_id: Optional[str] = None
    complete: bool = False      # True = đã quét hết vùng cần (gặp mốc / hết feed)

    def summary(self) -> dict:
        return {
            "engine": self.engine,
            "rejected": self.rejected[0] if self.rejected else None,
            "page_name": self.page_name,
            "found": self.found,
            "in_range": self.in_range,
            "new": self.new,
            "existing": self.existing,
            "skipped": self.skipped,
            "failed": self.failed,
            "stopped_reason": self.stopped_reason,
            "resume": self.since is not None,
            "since": self.since.isoformat() if self.since else None,
            "complete": self.complete,
        }


@dataclass
class Plan:
    term: str
    rng: dtrange.DateRange = field(default_factory=dtrange.DateRange)
    max_posts: int = 0
    lookback: int = DEFAULT_LOOKBACK
    search_all: bool = False
    exclusions: set = field(default_factory=set)
    excluded_terms: list = field(default_factory=list)
    exclude_sources: set = field(default_factory=set)
    keyword_id: Optional[int] = None
    group_id: Optional[int] = None
    keyword_match: str = "strict"  # strict | normal | loose | off
    newest_first: bool = True  # sort="recent": bài mới nhất trước (mặc định của UI)
    soft_stop_after: int = SOFT_STOP_AFTER
    stop_after_old: int = 12  # 12 bài LIÊN TIẾP cũ hơn mốc đầu khoảng thì dừng
    newest_probe: int = 150  # đọc chừng này bài mà lùi quá 1 ngày trước mốc, 0 bài in-range -> dừng
    engine: str = "auto"  # auto | graphql | browser (xem search_scan.crawl)
    page: Optional[str] = None  # quét feed của Page này (bỏ qua ô tìm kiếm)
    resume: bool = True  # Page: quét tiếp từ mốc lần trước (nếu có), không quét lại
    since: Optional[datetime] = None  # mốc thực tế đang dùng (đã trừ khoảng an toàn)


def build_plan(
    term: str,
    *,
    rng: Optional[dtrange.DateRange] = None,
    max_posts: Optional[int] = None,
    lookback: Optional[int] = None,
    search_all: bool = False,
    exclusions: Optional[list] = None,
    excluded_terms: Optional[list] = None,
    exclude_sources: Optional[list] = None,
    keyword_id: Optional[int] = None,
    group_id: Optional[int] = None,
    keyword_match: str = "strict",
    sort: str = "recent",
    newest_probe: Optional[int] = None,
    engine: str = "auto",
    page: Optional[str] = None,
    resume: bool = True,
) -> Plan:
    return Plan(
        term=term,
        rng=rng or dtrange.DateRange(),
        max_posts=max_posts or 0,
        lookback=lookback or DEFAULT_LOOKBACK,
        search_all=search_all,
        exclusions={str(x).strip() for x in (exclusions or []) if str(x).strip()},
        excluded_terms=[t.strip().lower() for t in (excluded_terms or []) if t and t.strip()],
        exclude_sources={s.strip().lower() for s in (exclude_sources or []) if s and s.strip()},
        keyword_id=keyword_id,
        group_id=group_id,
        keyword_match=keyword_match if keyword_match in ("strict", "normal", "loose", "off") else "strict",
        newest_first=sort != "relevance",
        engine=engine if engine in ENGINES else "auto",
        page=(page or "").strip() or None,
        resume=bool(resume),
        **({"newest_probe": newest_probe} if newest_probe else {}),
    )


def reach_note(rng: dtrange.DateRange, res: CrawlResult) -> Optional[str]:
    """Cảnh báo khi lọc khoảng cũ nhưng Facebook không trả bài nào trong khoảng đó.

    Facebook (qua thư viện) chỉ phân trang được tới một số tháng gần đây, nên lọc năm
    cũ thường ra 0 bài — cần nói rõ để người dùng không tưởng là lỗi tool.
    """
    if res.in_range or not rng.start or not res.found:
        return None
    return (
        f"Facebook không trả về bài nào trong khoảng {rng.describe()} cho từ khóa này "
        f"(đã đọc {res.found} bài, đều nằm ngoài khoảng — dừng vì {res.stopped_reason}). "
        "Kết quả tìm kiếm của Facebook chỉ đi tới một số tháng gần đây, nên bài cũ hơn "
        "thường không lấy được qua ô tìm kiếm."
    )


def _preview_note(plan: Plan, res: CrawlResult) -> Optional[str]:
    """Ghi chú cho preview: cảnh báo khi không có bài trong khoảng đã lọc."""
    if res.in_range:
        return None
    if plan.page and plan.since is not None and res.complete:
        return (
            f"Page “{res.page_name or plan.page}” không có bài mới sau mốc quét lần trước "
            f"({_fmt_vn(plan.since + PAGE_CURSOR_OVERLAP)}). Bỏ chọn “quét tiếp” nếu muốn "
            "quét lại toàn bộ khoảng đã chọn."
        )
    if plan.page and res.found:
        name = res.page_name or plan.page
        return (
            f"Page “{name}” không có bài nào trong khoảng {plan.rng.describe()} "
            f"(đã đọc {res.found} bài, đều nằm ngoài khoảng — dừng vì {res.stopped_reason})."
        )
    return reach_note(plan.rng, res)


# Các bộ lọc thời gian/loại trừ nhận từ API/CLI → build_plan
FILTER_KEYS = (
    "rng", "max_posts", "lookback", "search_all", "keyword_match", "sort",
    "exclusions", "excluded_terms", "exclude_sources", "keyword_id", "group_id",
    "engine", "page", "resume",
)

# Khi quét tiếp từ mốc lần trước, lùi lại 60 phút cho an toàn: bài đăng sát mốc có
# thể bị feed xếp lệch, hoặc mốc lưu từ lượt quét dở. Bài trùng vẫn được dedupe.
PAGE_CURSOR_OVERLAP = timedelta(minutes=60)


def page_cursor(page_ref: Optional[str]) -> Optional[dict]:
    """Mốc quét đã lưu cho Page (None nếu chưa quét lần nào)."""
    if not page_ref:
        return None
    key = browser_search.page_slug(page_ref).lower()
    if not key:
        return None
    with db.get_conn() as conn:
        cur = db.get_page_cursor(conn, key)
    if not cur or not cur.get("last_posted_at"):
        return None
    return {
        "source_key": cur["source_key"],
        "page_name": cur.get("page_name"),
        "last_scan_at": cur["last_scan_at"].isoformat() if cur.get("last_scan_at") else None,
        "last_posted_at": cur["last_posted_at"].isoformat(),
        "last_posted_text": _fmt_vn(cur["last_posted_at"]),
        "last_found": cur.get("last_found") or 0,
        "last_new": cur.get("last_new") or 0,
    }


def _fmt_vn(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(dtrange.VN_TZ)
    return dt.strftime("%d/%m/%Y %H:%M")


def _apply_page_cursor(plan: Plan) -> None:
    """Đặt `plan.since` = mốc quét lần trước (chỉ khi `resume` và mốc hợp với khoảng)."""
    if not plan.page or not plan.resume:
        return
    cur = page_cursor(plan.page)
    last = datetime.fromisoformat(cur["last_posted_at"]) if cur else None
    if not last:
        return
    since = last - PAGE_CURSOR_OVERLAP
    rng = plan.rng
    if not rng.empty:
        if rng.end and since >= rng.end:
            # Mốc đã ở sau khoảng đang chọn (vd chọn lại ngày cũ) -> phải quét đủ khoảng.
            return
        if rng.start and since < rng.start:
            since = rng.start
    plan.since = since


def _update_page_cursor(plan: Plan, res: CrawlResult, *, found: int, new: int) -> bool:
    """Lưu mốc quét mới cho Page sau khi quét xong.

    Chỉ cập nhật khi lượt quét này đã đi hết vùng cần (`res.complete`): nếu dừng vì
    `--max` hoặc lỗi lưu bài thì giữ mốc cũ để lần sau quét lại phần còn thiếu.
    """
    if not plan.page or not res.complete or not res.newest_posted_at:
        return False
    key = browser_search.page_slug(plan.page).lower()
    if not key:
        return False
    with db.get_conn() as conn:
        cur = db.get_page_cursor(conn, key)
        if cur and cur.get("last_posted_at") and cur["last_posted_at"] > res.newest_posted_at:
            return False  # quét lại khoảng cũ: không lùi mốc
        db.save_page_cursor(
            conn,
            key,
            page_url=browser_search.page_url(plan.page),
            page_name=res.page_name or plan.page,
            last_posted_at=res.newest_posted_at,
            last_fb_post_id=res.newest_fb_post_id,
            found=found,
            new=new,
            note=res.stopped_reason,
        )
    return True

# Cách lấy kết quả từ ô tìm kiếm Facebook:
#   graphql : gọi GraphQL như thư viện facebook-gql-scraper (đầy đủ dữ liệu)
#   browser : mở trang search/posts bằng Chromium (Playwright) — dùng khi GraphQL bị chặn
#   auto    : thử graphql, nếu Facebook trả error 1357004 thì tự chuyển sang browser
ENGINES = ("auto", "graphql", "browser")


# ------------------------------------------------------------------ ô tìm kiếm
# Facebook không sắp xếp kết quả tìm kiếm theo thời gian (mặc định là độ liên quan, bài
# cũ/mới chen lẫn). Muốn "bài mới nhất / hôm nay" phải gửi filter `recent_posts` — đúng
# filter của nút "Bài viết mới đây" trên giao diện FB.
_PUBLIC_POSTS = json.dumps({"name": "merged_public_posts", "args": ""})
_RECENT_POSTS = json.dumps({"name": "recent_posts", "args": ""})


def _sorted_search(ctx, term: str, newest_first: bool):
    """Iterator kết quả tìm kiếm (bài) — `newest_first=True` thì lấy bài mới trước."""
    if not newest_first:
        return fgs.SearchEngine(ctx).search(term, filter_type="post")

    # Dùng đúng query GraphQL của thư viện nhưng thêm filter recent_posts + tab Bài viết.
    query = _SerpQuery(ctx)

    def request_fn(cursor):
        variables = dict(query.VARIABLES)
        args = dict(query.VARIABLES["args"])
        args["text"] = term
        args["experience"] = {**args["experience"], "type": "POSTS_TAB"}
        args["filters"] = [_PUBLIC_POSTS, _RECENT_POSTS]
        variables["args"] = args
        variables["cursor"] = cursor
        return ctx.post(
            operation_name=query.OPERATION_NAME,
            reference_page_url=query.REFERENCE_PAGE_URL,
            variables=variables,
        ).text

    def page_wrapper(parsed):
        # Trả nguyên dict Post của parser (giống SearchEngine.search), KHÔNG bọc lại
        # thành {"data": ...} — Post cần đủ field (text/creation_time/url/attachments).
        data = parsed["parsed_data"]
        return list(data["posts"]), data["page_info"]

    return fgs.PaginatedIterator(
        request_fn=request_fn,
        parser=ctx.parse_graphql_response,
        page_wrapper=page_wrapper,
        item_wrapper=lambda data: fgs.Post(ctx, data),
        iterator_type="serp_search_recent",
        query_variables={"text": term, "filter_type": "post", "recent": True},
    )


def _ctx_or_browser():
    """Context GraphQL (nếu tạo được). Thiếu/hỏng cookies thì trả None để vẫn thử browser."""
    try:
        from . import context as fbctx

        return fbctx.make_context(quiet=True)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ! Không tạo được phiên GraphQL: {exc}")
        return None


def _log(msg: str) -> None:
    print(msg, flush=True)


_SEARCH_REJECTED = "Facebook từ chối yêu cầu tìm kiếm"


def _search_error(res: CrawlResult, raw) -> Optional[str]:
    """Facebook trả trang lỗi (`for (;;);{"error":1357004,...}`) thay vì kết quả.

    Thư viện coi payload lỗi là "không có dữ liệu" -> iterator rỗng, nên nếu không kiểm
    tra thì mọi từ khóa đều ra 0 bài như thể Facebook hết kết quả. Trả mô tả lỗi để
    người dùng biết cần làm gì (thường là phiên bị chặn / cookies thiếu `c_user`, `xs`).
    """
    if not isinstance(raw, str):
        return None
    tag = '"error"'
    idx = raw.find(tag)
    # Trang lỗi của Facebook luôn bắt đầu bằng "for (;;);{" rồi tới error ngay đầu payload;
    # còn kết quả thật có thể chứa chữ "error" ở giữa (tên field, thông báo...) nên chỉ
    # kiểm tra phần đầu.
    if idx < 0 or idx > 260 or not raw.lstrip().startswith("for (;;);"):
        return None
    detail = ""
    try:
        start = raw.index("{")
        obj = json.loads(raw[start : raw.index("}", idx) + 1])
        detail = f"{obj.get('errorSummary') or ''} {obj.get('errorDescription') or ''}".strip().rstrip(".")
        if obj.get("error"):
            detail = f"error {obj['error']}: {detail}"
    except (ValueError, AttributeError):
        pass
    res.errors.append(f"{_SEARCH_REJECTED}: {detail}. (Phiên đăng nhập bị chặn?)".strip())
    return res.errors[-1]


def _sleep(delay: Optional[float] = None) -> None:
    if delay is None:
        delay = config.REQUEST_DELAY_SEC
    if delay > 0:
        time.sleep(delay * random.uniform(0.8, 1.4))


# -------------------------------------------------------------------- lõi quét
def _to_hit(ctx, post: fgs.Post, url: Optional[str], plan: Plan, conn) -> SearchHit:
    # Đường nhanh: nguồn + id lấy thẳng từ URL permalink (không gọi GraphQL).
    # `_source_of` chỉ được dùng khi URL không đọc được — mỗi lần gọi nó có thể tốn
    # ~1s vì thư viện tự fetch thêm dữ liệu bài/group.
    src = _source_from_url(url)
    if src is None:
        src = _source_of(ctx, post, url) or {}
    posted_at = normalize.to_ts(_safe_attr(post, "creation_time"))
    existing = db.get_post_by_fbid(conn, str(getattr(post, "id")))
    text = _safe_attr(post, "text")
    level, _missing = score_match(text, plan.term)
    return SearchHit(
        fb_post_id=str(getattr(post, "id")),
        posted_at=posted_at,
        url=url,
        text=text,
        author_name=_safe_attr(_safe_attr(post, "owner"), "name"),
        source_type=src.get("source_type") or "unknown",
        source_fb_id=src.get("source_fb_id"),
        source_name=src.get("source_name") or None,
        in_range=plan.rng.contains(posted_at),
        existing_id=existing["id"] if existing else None,
        level=level,
        group_url=src.get("source_url") if src.get("source_type") == "group" else None,
        post=post,
    )


def _needs_post(post) -> bool:
    try:
        return _needs_detail(post)
    except Exception:  # noqa: BLE001
        return True


def _skip_reason(hit: SearchHit, plan: Plan) -> Optional[str]:
    """Lý do KHÔNG lưu bài này (None = nên lưu)."""
    if hit.existing_id:
        return "already_in_db"
    if not hit.in_range:
        return "out_of_range"
    if not matches(hit, plan):
        return "not_match"
    if hit.fb_post_id in plan.exclusions:
        return "excluded_id"
    low = (hit.text or "").lower()
    if any(t in low for t in plan.excluded_terms):
        return "excluded_term"
    if hit.source_fb_id and hit.source_fb_id.lower() in plan.exclude_sources:
        return "excluded_source"
    if not plan.search_all and not plan.page and hit.source_type != "group":
        return "not_group"
    if hit.source_type not in ("group", "page", "user"):
        return "no_source"
    return None


def _iter_hits(ctx, plan: Plan, res: CrawlResult, conn):
    """Lặp qua kết quả Facebook, yield (hit, lý_do_bỏ_qua|None). Cập nhật `res`."""
    it = _sorted_search(ctx, plan.term, newest_first=plan.newest_first)
    # Bọc `_request_fn` để xem payload thô: nếu Facebook trả trang lỗi (`for (;;);{"error":
    # 1357004,...}`) thì thư viện coi như "không có dữ liệu" -> iterator rỗng. Không kiểm tra
    # thì mọi từ khóa đều báo 0 bài y như Facebook hết kết quả.
    raw_request = it._request_fn

    def _request_checked(cursor):
        raw = raw_request(cursor)
        if _search_error(res, raw):
            res.stopped_reason = res.errors[-1]
        return raw

    it._request_fn = _request_checked
    seen: set[str] = set()
    soft_skips = 0

    while True:
        if res.stopped_reason.startswith(_SEARCH_REJECTED):
            return
        try:
            raw = next(it)
        except StopIteration:
            if not res.stopped_reason.startswith(_SEARCH_REJECTED):
                res.stopped_reason = "hết trang kết quả"
            return
        except (AttributeError, TypeError, KeyError) as exc:
            # Lỗi parser của thư viện trên vài bài lạ (vd comment có attachment null):
            # bỏ qua rồi đọc tiếp, không làm hỏng cả lượt quét.
            res.failed += 1
            res.errors.append(f"bỏ qua 1 bài lỗi parser: {type(exc).__name__}: {exc}")
            if config.DEBUG:
                traceback.print_exc()
            continue
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"đọc trang kết quả: {type(exc).__name__}: {exc}")
            res.stopped_reason = "lỗi khi đọc kết quả"
            if config.DEBUG:
                traceback.print_exc()
            return

        res.found += 1
        if res.found % PAGE_BATCH == 0:
            _log(f"  ... đọc {res.found} bài ({res.in_range} trong khoảng, {res.new} lưu mới)")
            conn.commit()

        # Chặn "lan man": nếu vẫn chưa thấy bài nào trong khoảng dù đã đọc nhiều bài và bài
        # cũ nhất đã lùi hẳn trước mốc đầu khoảng, thì đi sâu hơn cũng không gặp -> dừng.
        cutoff0 = plan.rng.row_cutoff()
        if (
            plan.newest_first
            and cutoff0
            and res.in_range == 0
            and res.oldest_seen
            and res.oldest_seen < cutoff0 - timedelta(days=1)
            and res.found >= plan.newest_probe
        ):
            res.stopped_reason = (
                f"đã đọc {res.found} bài và lùi tới {res.oldest_seen.strftime('%Y-%m-%d')} "
                f"mà không có bài trong khoảng — dừng"
            )
            return

        if plan.max_posts and res.new >= plan.max_posts:
            res.stopped_reason = f"đạt giới hạn {plan.max_posts} bài mới"
            return
        if res.found > plan.lookback:
            res.stopped_reason = f"đã đọc {plan.lookback} bài (giới hạn lookback)"
            return

        try:
            post = raw
            if _needs_post(post):
                post = fgs.Post.from_post_id(ctx, str(raw.id))
            hit = _to_hit(ctx, post, _safe_attr(post, "url") or _safe_attr(raw, "url"), plan, conn)
        except Exception as exc:  # noqa: BLE001
            res.failed += 1
            res.errors.append(f"bài {_safe_attr(raw, 'id')}: {type(exc).__name__}: {exc}")
            _log(f"  X bài {_safe_attr(raw, 'id')}: {type(exc).__name__}: {exc}")
            if config.DEBUG:
                traceback.print_exc()
            continue

        if hit.in_range:
            res.in_range += 1
        if hit.posted_at:
            res.times.append(hit.posted_at)

        # Dừng khi đã chắc chắn qua khỏi khoảng: kết quả "mới trước" của Facebook chỉ xấp
        # xỉ theo giờ trong cùng một ngày (15:31, 15:18, 15:23...) nên phải thấy nhiều bài
        # cũ hơn mốc đầu khoảng, và bài cũ nhất đã lùi hẳn trước mốc đó.
        cutoff = plan.rng.row_cutoff()
        if plan.newest_first and cutoff and hit.posted_at and hit.fb_post_id not in seen:
            if hit.posted_at < cutoff:
                res.past += 1
                res.oldest_seen = (
                    hit.posted_at if res.oldest_seen is None else min(res.oldest_seen, hit.posted_at)
                )
                # Đã lấy được ít nhất 1 bài trong khoảng VÀ đã lùi qua khỏi mốc đầu khoảng
                # từ 2 ngày trở lên -> coi như hết vùng cần lọc, dừng.
                if res.in_range and res.oldest_seen < cutoff - timedelta(days=2):
                    res.stopped_reason = f"đã qua bài cũ hơn {cutoff.strftime('%Y-%m-%d')}"
                    return
            else:
                res.past = 0  # gặp bài trong khoảng/mới hơn -> reset chuỗi bài cũ

        reason = "duplicate" if hit.fb_post_id in seen else _skip_reason(hit, plan)
        seen.add(hit.fb_post_id)

        if reason in SOFT_REASONS:
            soft_skips += 1
            if soft_skips >= plan.soft_stop_after:
                res.stopped_reason = f"gặp {soft_skips} bài đã có/ngoài khoảng — dừng"
                yield hit, reason
                return

        yield hit, reason
        _sleep()


def _save_hit(ctx, conn, plan: Plan, hit: SearchHit, download_media: bool) -> tuple[int, bool]:
    """Lưu 1 bài vào scan_posts. Trả (post_id, is_new)."""
    post = hit.post
    row = normalize.to_row(post)
    row.update(
        keyword_id=plan.keyword_id,
        source_type=hit.source_type,
        source_fb_id=hit.source_fb_id,
        source_name=hit.source_name,
        source_url=hit.url,
    )
    if hit.source_type == "group" and hit.source_fb_id:
        gid = db.upsert_group(
            conn,
            hit.source_fb_id,
            slug=None,
            name=hit.source_name,
            group_url=hit.group_url or f"https://www.facebook.com/groups/{hit.source_fb_id}",
        )
        post_id, is_new = db.upsert_post(conn, gid, row)
    else:
        post_id, is_new = db.upsert_post(conn, None, row)

    if is_new and download_media and row["_media"]:
        saved = []
        for m in row["_media"]:
            got = media.download(ctx, m["source_url"], str(getattr(post, "id")), m["position"])
            if got:
                saved.append(
                    {**m, "file_path": got["file_path"], "bytes": got["bytes"],
                     "downloaded_at": db.utcnow()}
                )
            _sleep(0.3)
        db.replace_media(conn, post_id, saved)
    return post_id, is_new


def _save_browser_hit(conn, plan: Plan, hit: SearchHit) -> tuple[int, bool]:
    """Lưu bài đọc từ trang tìm kiếm qua browser (không tải ảnh — trang chỉ có thumbnail)."""
    row = browser_search.post_row(hit.browser, keyword_id=plan.keyword_id)
    row["content"] = row.get("content") or hit.text
    row["permalink"] = row.get("permalink") or hit.url
    return db.insert_browser_post(conn, None, row)


def _engine_hits(ctx, plan: Plan, res: CrawlResult, conn):
    """Chọn engine: graphql / browser, hoặc auto (thử GraphQL rồi tự chuyển khi bị chặn)."""
    if plan.page:
        # Feed Page luôn đọc bằng browser: GraphQL trực tiếp của tool bị chặn 1357004.
        yield from _browser_hits(plan, res, conn)
        return
    if plan.engine == "browser":
        yield from _browser_hits(plan, res, conn)
        return
    if plan.engine == "auto":
        yield from _with_browser_fallback(ctx, plan, res, conn)
        return
    if ctx is None:
        msg = "Không có phiên Facebook cho GraphQL — export lại facebook_cookies.json hoặc dùng engine=browser"
        res.errors.append(msg)
        res.stopped_reason = f"lỗi: {msg}"
        return
    yield from _iter_hits(ctx, plan, res, conn)


def _with_browser_fallback(ctx, plan: Plan, res: CrawlResult, conn):
    """GraphQL trước; nếu Facebook từ chối ngay trang đầu thì đọc lại bằng browser."""
    produced = 0
    if ctx is not None:
        for hit, reason in _iter_hits(ctx, plan, res, conn):
            produced += 1
            yield hit, reason
    if produced or not res.stopped_reason.startswith(_SEARCH_REJECTED):
        return
    _log(
        "  ! GraphQL bị Facebook từ chối (error 1357004) — chuyển sang đọc trang tìm kiếm "
        "bằng browser (Playwright)."
    )
    # Chưa bài nào được xử lý: xoá kết quả của lần thử GraphQL rồi chạy lại bằng browser.
    res.errors.clear()
    res.stopped_reason = ""
    res.times.clear()
    res.found = res.in_range = res.new = res.existing = res.skipped = res.failed = 0
    res.oldest_seen = None
    res.past = 0
    yield from _browser_hits(plan, res, conn)


def _browser_hits(plan: Plan, res: CrawlResult, conn):
    """Đọc ô tìm kiếm Facebook bằng browser, yield (hit, lý do) theo đúng luật lọc."""
    if not browser_search.available():
        msg = (
            "Facebook chặn GraphQL (error 1357004) và chưa cài Playwright để đọc trang tìm "
            "kiếm — chạy: pip install playwright && python -m playwright install chromium"
        )
        res.errors.append(msg)
        res.stopped_reason = f"lỗi: {msg}"
        return

    res.engine = "browser"
    scrolls = max(10, min(200, plan.lookback // 10 or 30))
    if plan.page:
        url = browser_search.page_url(plan.page)
        extra = f" — tiếp từ {_fmt_vn(plan.since)}" if plan.since else ""
        _log(f"  -> page: mở {url} ({scrolls} lần cuộn tối đa){extra}")
        try:
            crawl = browser_search.crawl_page(
                plan.page,
                rng=plan.rng,
                since=plan.since,
                scrolls=scrolls,
                max_posts=plan.max_posts,
            )
        except browser_search.BrowserSearchError as exc:
            res.errors.append(str(exc))
            res.stopped_reason = f"lỗi: {exc}"
            return
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"page: {type(exc).__name__}: {exc}")
            res.stopped_reason = f"lỗi: page {type(exc).__name__}: {exc}"
            if config.DEBUG:
                traceback.print_exc()
            return
        res.page_name = crawl.page_name
        res.page = plan.page
        res.since = plan.since
        res.complete = crawl.complete
        newest = max(
            (h for h in crawl.hits if h.posted_at),
            key=lambda h: h.posted_at,
            default=None,
        )
        if newest:
            res.newest_posted_at = newest.posted_at
            res.newest_fb_post_id = newest.fb_post_id
    else:
        _log(f"  -> browser: mở {browser_search.SEARCH_URL.format(q=plan.term)} ({scrolls} lần cuộn)")
        try:
            crawl = browser_search.crawl(
                plan.term, rng=plan.rng, scrolls=scrolls, max_posts=plan.max_posts,
            )
        except browser_search.BrowserSearchError as exc:
            res.errors.append(str(exc))
            res.stopped_reason = f"lỗi: {exc}"
            return
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"browser: {type(exc).__name__}: {exc}")
            res.stopped_reason = f"lỗi: browser {type(exc).__name__}: {exc}"
            if config.DEBUG:
                traceback.print_exc()
            return

    res.errors.extend(crawl.errors)
    res.rejected.extend(crawl.errors)
    res.stopped_reason = crawl.stopped_reason or f"browser đã cuộn {crawl.scrolls} lần"
    label = "page" if plan.page else "browser"
    _log(f"  -> {label} đọc được {len(crawl.hits)} bài (cuộn {crawl.scrolls} lần)")
    new_count = 0
    for hit in crawl.hits:
        res.found += 1
        if hit.posted_at:
            res.times.append(hit.posted_at)
            res.oldest_seen = (
                hit.posted_at if res.oldest_seen is None else min(res.oldest_seen, hit.posted_at)
            )
        if res.found > plan.lookback:
            res.stopped_reason = f"đã đọc {plan.lookback} bài (giới hạn lookback)"
            return
        sh = _browser_to_hit(conn, plan, hit)
        if sh.in_range:
            res.in_range += 1
        reason = _skip_reason(sh, plan)
        if reason is None:
            new_count += 1
            if plan.max_posts and new_count > plan.max_posts:
                res.stopped_reason = f"đạt giới hạn {plan.max_posts} bài mới"
                return
            yield sh, None
            continue
        if reason == "already_in_db":
            res.existing += 1
        else:
            res.skipped += 1
        yield sh, reason


def _browser_to_hit(conn, plan: Plan, hit) -> SearchHit:
    """BrowserHit -> SearchHit (chấm mức khớp từ khóa, tra DB xem đã có chưa)."""
    existing = db.get_post_by_source_path(conn, hit.source_path) or db.get_post_by_fbid(
        conn, hit.fb_post_id
    )
    level, _missing = score_match(hit.content, plan.term)
    return SearchHit(
        fb_post_id=hit.fb_post_id,
        posted_at=hit.posted_at,
        url=hit.permalink,
        text=hit.content,
        author_name=hit.author_name,
        source_type=hit.source_type,
        source_fb_id=hit.source_fb_id,
        source_name=hit.source_name,
        in_range=plan.rng.contains(hit.posted_at),
        existing_id=existing["id"] if existing else None,
        level=level,
        browser=hit,
    )


def _crawl(ctx, plan: Plan, *, save: bool, download_media: bool):
    """Chạy một lượt tìm. `save=True` thì lưu bài vào DB, `save=False` chỉ đếm."""
    res = CrawlResult(term=plan.term)
    title = f'Page "{plan.page}"' if plan.page else f'Ô tìm kiếm: "{plan.term}"'
    _log(
        f"\n=== {title} — {plan.rng.describe()}"
        f"{' — CHỈ KIỂM TRA (không lưu)' if not save else ''} ==="
    )
    outcomes: list[Outcome] = []
    # Engine đọc cả trang (browser) đếm found/in_range/existing/skipped khi lặp bài;
    # engine GraphQL (feed) chỉ đếm sau khi lọc. Cờ dưới để không đếm hai lần.
    with db.get_conn() as conn:
        for hit, reason in _engine_hits(ctx, plan, res, conn):
            counted = res.engine == "browser"
            if reason is not None:
                if not counted:
                    if reason == "already_in_db":
                        res.existing += 1
                    else:
                        res.skipped += 1
                outcomes.append(
                    Outcome(hit, reason,
                            post_id=hit.existing_id if reason == "already_in_db" else None)
                )
                continue

            if not save:
                res.new += 1  # "sẽ lưu"
                outcomes.append(Outcome(hit))
                continue

            try:
                if hit.browser is not None:
                    post_id, is_new = _save_browser_hit(conn, plan, hit)
                else:
                    post_id, is_new = _save_hit(ctx, conn, plan, hit, download_media)
            except Exception as exc:  # noqa: BLE001
                res.failed += 1
                res.errors.append(f"lưu bài {hit.fb_post_id}: {type(exc).__name__}: {exc}")
                _log(f"  X lưu bài {hit.fb_post_id}: {type(exc).__name__}: {exc}")
                if config.DEBUG:
                    traceback.print_exc()
                outcomes.append(Outcome(hit, "error"))
                continue

            if is_new:
                res.new += 1
                outcomes.append(Outcome(hit, post_id=post_id))
            else:
                if not counted:
                    res.existing += 1
                outcomes.append(Outcome(hit, "already_in_db", post_id=post_id))
        conn.commit()

    if not res.stopped_reason:
        res.stopped_reason = "xong"
    if res.found == 0 and res.errors:
        # Không đọc được bài nào mà có lỗi: đó là lỗi phiên/yêu cầu, không phải "không có kết quả".
        res.stopped_reason = f"lỗi: {res.errors[0]}"
        _log(f"  ! {res.stopped_reason}")
    _log(
        f"  -> FB trả {res.found} bài, trong khoảng {res.in_range}: "
        f"mới {res.new}, đã có {res.existing}, bỏ qua {res.skipped}, lỗi {res.failed} "
        f"({res.stopped_reason})"
    )
    return res, outcomes


# ------------------------------------------------------------------- preview
def preview_search(ctx, term: str, *, sample: int = 30, **filters) -> dict:
    """Xem trước: Facebook trả gì, bao nhiêu bài sẽ lưu. KHÔNG ghi DB."""
    plan = build_plan(term, **filters)
    _apply_page_cursor(plan)
    if ctx is None and not plan.page:
        ctx = _ctx_or_browser()
    res, outcomes = _crawl(ctx, plan, save=False, download_media=False)

    counts: dict[str, int] = {}
    by_level: dict[str, int] = {}
    items: list[dict] = []
    accepted = accepted_levels(plan.keyword_match)
    for o in outcomes:
        key = o.reason or "saved"
        counts[key] = counts.get(key, 0) + 1
        by_level[o.hit.level] = by_level.get(o.hit.level, 0) + 1
        # Chỉ trả bài ĐỦ ĐIỀU KIỆN: đúng từ khóa, đúng khoảng thời gian, không bị loại trừ.
        # Bài ngoài khoảng / không chứa từ khóa chỉ còn trong `counts` (số liệu ẩn).
        if o.reason is not None and o.reason != "already_in_db":
            continue
        if len(items) < sample:
            items.append({**o.hit.brief(), "keep": o.reason is None,
                          "existing": o.reason == "already_in_db",
                          "reason": o.reason,
                          "match_accepted": o.hit.level in accepted or plan.keyword_match == "off",
                          "level_text": MATCH_LEVEL_TEXT.get(o.hit.level, o.hit.level),
                          "reason_text": SKIP_REASONS.get(o.reason or "", "Sẽ lưu")})

    return {
        "term": term or (res.page_name or plan.page or ""),
        "page": plan.page,
        "page_name": res.page_name,
        "range": plan.rng.describe(),
        "range_from": plan.rng.start.isoformat() if plan.rng.start else None,
        "range_to": plan.rng.end.isoformat() if plan.rng.end else None,
        "engine": res.engine,
        "would_save": res.new,
        "existing": res.existing,
        "skipped": res.skipped,
        "failed": res.failed,
        "found": res.found,
        "in_range": res.in_range,
        "stopped_reason": res.stopped_reason,
        "note": _preview_note(plan, res),
        "resume": res.since is not None,
        "since": plan.since.isoformat() if plan.since else None,
        "since_text": _fmt_vn(plan.since) if plan.since else None,
        "cursor": page_cursor(plan.page),
        "keyword_match": plan.keyword_match,
        "match_counts": {MATCH_LEVEL_TEXT.get(k, k): v for k, v in by_level.items()},
        "errors": res.errors[:10],
        "error": res.rejected[0] if res.rejected else (res.errors[0] if res.errors else None),
        "hidden_counts": {
            SKIP_REASONS.get(k, k): v
            for k, v in counts.items()
            if k not in ("saved", "already_in_db")
        },
        "counts": {SKIP_REASONS.get(k, k): v for k, v in counts.items() if k in ("saved", "already_in_db")},
        "items": items,
        "sample_size": sample,
    }


# ------------------------------------------------------------------- review
def review_search(
    ctx,
    term: str,
    *,
    request_id: Optional[int] = None,
    created_by: str = "review",
    download_media: bool = True,
    **filters,
) -> dict:
    """Quét + ghi `scan_reviews` để đối chiếu với Facebook. Trả dict tổng hợp."""
    plan = build_plan(term, **filters)
    _apply_page_cursor(plan)
    if ctx is None and not plan.page:
        ctx = _ctx_or_browser()
    res, outcomes = _crawl(ctx, plan, save=True, download_media=download_media)

    new_ids: list[int] = []
    existing_ids: list[int] = []
    skipped: list[dict] = []
    for o in outcomes:
        if o.reason is None:
            if o.post_id:
                new_ids.append(o.post_id)
            continue
        if o.reason in ("already_in_db", "duplicate"):
            if o.post_id:
                existing_ids.append(o.post_id)
            else:
                skipped.append({**{k: o.hit.brief()[k] for k in ("fb_post_id", "source_name")},
                                "reason": o.reason, "reason_text": SKIP_REASONS[o.reason]})
            continue
        skipped.append(
            {
                "fb_post_id": o.hit.fb_post_id,
                "reason": o.reason,
                "reason_text": SKIP_REASONS.get(o.reason, o.reason),
                "url": o.hit.url,
                "posted_at": o.hit.posted_at.isoformat() if o.hit.posted_at else None,
                "source_name": o.hit.source_name,
                "snippet": (o.hit.text or "")[:160],
            }
        )

    with db.get_conn() as conn:
        review_id = db.save_review(
            conn,
            {
                "request_id": request_id,
                "keyword_id": plan.keyword_id,
                "group_id": plan.group_id,
                "term": term,
                "date_from": plan.rng.start,
                "date_to": plan.rng.end,
                "found": res.found,
                "in_range": res.in_range,
                "new_ids": new_ids,
                "existing_ids": existing_ids,
                "skipped": skipped,
                "search_all": plan.search_all,
                "created_by": created_by,
            },
        )
    note = _preview_note(plan, res)
    cursor_updated = _update_page_cursor(plan, res, found=res.found, new=res.new)
    _log(
        f"  OK review #{review_id}: FB trả {res.found} bài, trong khoảng {res.in_range}, "
        f"lưu mới {len(new_ids)}, đã có {len(existing_ids)}, bỏ qua {len(skipped)}"
    )
    if note:
        _log(f"  ! {note}")
    if cursor_updated:
        _log(f"  -> mốc quét Page: {_fmt_vn(res.newest_posted_at)}")
    return {
        "review_id": review_id,
        "term": term,
        "page": plan.page,
        "page_name": res.page_name,
        "range": plan.rng.describe(),
        "note": note,
        **res.summary(),
        "cursor": page_cursor(plan.page),
        "cursor_updated": cursor_updated,
        "newest_posted_text": _fmt_vn(res.newest_posted_at),
        "new_count": len(new_ids),
        "existing_count": len(existing_ids),
        "skipped_count": len(skipped),
        "new_ids": new_ids[:500],
    }


# --------------------------------------------------------------- chạy yêu cầu
def run_request(ctx, req: dict) -> dict:
    """Thực thi yêu cầu quét trong `scan_requests` (group hoặc từ khóa) + ghi job."""
    rng = dtrange.DateRange(
        start=req.get("date_from"),
        end=req.get("date_to"),
    )
    filters = {
        "rng": rng,
        "max_posts": req.get("max_posts") or 0,
        "lookback": req.get("lookback"),
        "search_all": bool(req.get("search_all")),
        "keyword_match": req.get("keyword_match") or "strict",
        "sort": req.get("sort") or "recent",
        "exclusions": req.get("exclusions") or [],
        "excluded_terms": req.get("excluded_terms") or [],
        "exclude_sources": req.get("exclude_sources") or [],
        "keyword_id": req.get("keyword_id"),
        "group_id": None if req.get("keyword_id") else req.get("group_id"),
        "engine": req.get("engine") or "auto",
        "page": req.get("page"),
    }
    filters = {k: v for k, v in filters.items() if k in FILTER_KEYS}

    with db.get_conn() as conn:
        job_id = db.start_job(conn, req.get("group_id"), req.get("requested_by") or "api")

    term = req.get("_term")
    if not term and req.get("page"):
        term = req["page"]
    if not term:
        with db.get_conn() as conn:
            if req.get("keyword_id"):
                kw = db.get_keyword(conn, req["keyword_id"])
                term = kw["term"] if kw else None
            elif req.get("group_id"):
                grp = db.get_group(conn, req["group_id"])
                term = grp["name"] if grp else f"group {req['group_id']}"
    term = term or ""

    status = "done"
    error = None
    note = None
    summary: dict = {}
    try:
        dl = req.get("download_media")
        res, _outcomes = _crawl(
            ctx, build_plan(term, **filters), save=True,
            download_media=config.DOWNLOAD_MEDIA if dl is None else bool(dl),
        )
        summary = res.summary()
        note = reach_note(rng, res)
        if note:
            _log(f"  ! {note}")
        if res.errors:
            error = ("; ".join(res.errors[:5])
                     + (f" (+{len(res.errors) - 5} lỗi khác)" if len(res.errors) > 5 else ""))[:2000]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        status = "error"
        _log(f"  X Lỗi: {error}")
        if config.DEBUG:
            traceback.print_exc()

    with db.get_conn() as conn:
        db.finish_job(
            conn, job_id, status=status,
            found=summary.get("found", 0),
            new=summary.get("new", 0),
            updated=summary.get("existing", 0),
            error=error,
        )
    return {"status": status, "job_id": job_id, **summary, "note": note, "error": error}


__all__ = [
    "build_plan",
    "preview_search",
    "review_search",
    "run_request",
    "reach_note",
    "score_match",
    "matches",
    "MATCH_LEVEL_TEXT",
    "strip_accents",
    "SearchHit",
    "SKIP_REASONS",
    "DEFAULT_LOOKBACK",
]
