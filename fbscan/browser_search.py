from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from . import config

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SEARCH_URL = "https://www.facebook.com/search/posts/?q={q}"

# Link bài: /groups/<group>/posts/<id>, /groups/<id>/permalink/<id>,
# /<user>/posts/pfbid... — nhóm "chủ thể" là group id/slug hoặc username Page/người.
_POST_PATH = re.compile(
    r"^/(?:(groups)/([^/]+)/(?:posts|permalink)|([^/]+)/(?:posts|videos))/([A-Za-z0-9]+)"
)
# Link bài dạng ID số cổ điển: story_fbid=123&id=456
_STORY_FBID = re.compile(r"[?&]story_fbid=(\d+)[^#]*?[?&]id=(\d+)")

_DATE_TEXT = re.compile(
    r"(?:Thứ [a-zA-Zà-ỹ]+,\s*)?(\d{1,2})\s+Tháng\s+(\d{1,2})(?:,?\s*(\d{4}))?"
    r"(?:[^0-9]{0,20}(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)
_RELATIVE = re.compile(r"(\d+)\s*(giây|phút|giờ|ngày|tuần|tháng|năm)\s*trước", re.IGNORECASE)


class BrowserSearchError(RuntimeError):
    """Không mở được trang tìm kiếm / bị bắt đăng nhập / chưa cài Playwright."""


@dataclass
class BrowserHit:
    fb_post_id: str
    source_path: str
    permalink: str
    content: Optional[str]
    author_name: Optional[str]
    source_type: str  # group | page | user
    source_fb_id: Optional[str]
    source_name: Optional[str]
    posted_at: Optional[datetime]
    has_media: bool = False
    engine: str = "browser-html"  # browser-html | browser-dom

    def brief(self) -> dict:
        return {
            "fb_post_id": self.fb_post_id,
            "url": self.permalink,
            "permalink": self.permalink,
            "text": self.content,
            "snippet": (self.content or "")[:300],
            "author_name": self.author_name,
            "source_type": self.source_type,
            "source_fb_id": self.source_fb_id,
            "source_name": self.source_name,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
        }


@dataclass
class BrowserCrawl:
    hits: list[BrowserHit] = field(default_factory=list)
    stopped_reason: str = ""
    errors: list[str] = field(default_factory=list)
    scrolls: int = 0
    pages: int = 0
    page_name: str = ""  # tên Page khi quét feed Page
    cutoff: Optional[datetime] = None  # bài cũ hơn mốc này thì dừng (mốc ngày/mốc quét)
    complete: bool = False  # True = đã quét hết vùng cần (gặp mốc hoặc hết feed)


# ------------------------------------------------------------------ cookie file
def _cookies() -> list[dict]:
    path = Path(config.COOKIES_FILE)
    if not path.exists():
        raise BrowserSearchError(f"Không thấy file cookies: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrowserSearchError(f"Không đọc được cookies: {exc}") from exc
    if isinstance(raw, dict):
        raw = raw.get("cookies") or []
    out, seen = [], set()
    for c in raw:
        name = c.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        item = {
            "name": name,
            "value": c.get("value") or "",
            "domain": c.get("domain") or ".facebook.com",
            "path": c.get("path") or "/",
            "secure": True,
        }
        exp = c.get("expirationDate")
        if exp and not c.get("session"):
            item["expires"] = float(exp)
        out.append(item)
    if not any(c["name"] == "c_user" for c in out):
        raise BrowserSearchError("Cookies thiếu 'c_user' — export lại facebook_cookies.json")
    return out


# ------------------------------------------------------------------ link parsing
def parse_post_link(href: str) -> Optional[dict]:
    """Tách link bài Facebook -> nguồn + id chuẩn hoá.

    `/groups/<g>/posts/<id>` , `/<user>/posts/<fbid>` -> source_path `<chủ thể>/posts/<id>`
    """
    if not href:
        return None
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    path = re.sub(r"^https?://[^/]+", "", href).split("?")[0].split("#")[0]
    m = _POST_PATH.match(path)
    if m:
        is_group, group_ref, user_ref, fid = m.groups()
        owner = group_ref if is_group else user_ref
        return {
            "source_path": f"{owner}/posts/{fid}",
            "source_type": "group" if is_group else "user",
            "source_fb_id": owner,
            "fb_post_id": f"{owner}_posts_{fid}",
        }
    m = _STORY_FBID.search(href)
    if m:
        fid, owner = m.group(1), m.group(2)
        return {
            "source_path": f"{owner}/posts/{fid}",
            "source_type": "user",
            "source_fb_id": owner,
            "fb_post_id": f"{owner}_posts_{fid}",
        }
    return None


# ------------------------------------------------------------------ datetime
def _relative_to_dt(text: str, now: datetime) -> Optional[datetime]:
    m = _RELATIVE.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {
        "giây": timedelta(seconds=n),
        "phút": timedelta(minutes=n),
        "giờ": timedelta(hours=n),
        "ngày": timedelta(days=n),
        "tuần": timedelta(weeks=n),
        "tháng": timedelta(days=30 * n),
        "năm": timedelta(days=365 * n),
    }[unit]
    return now - delta


def parse_date_text(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(VN_TZ)
    low = text or ""
    rel = _relative_to_dt(low, now)
    if rel:
        return rel
    if "hôm qua" in low.lower():
        return now - timedelta(days=1)
    m = _DATE_TEXT.search(low)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else now.year
    hour = int(m.group(4)) if m.group(4) else 12
    minute = int(m.group(5)) if m.group(5) else 0
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
    except ValueError:
        return None
    if not m.group(3) and dt > now + timedelta(days=1):
        dt = dt.replace(year=year - 1)
    return dt


def _ts_to_dt(value) -> Optional[datetime]:
    """creation_time của Facebook (unix giây) -> datetime."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(VN_TZ)


_FEED_STORY = re.compile(
    r'\{"__typename":"(?:Story|CometStory)","__isFeedUnit":"(?:Story|CometStory)"'
    r'|\{"__isFeedUnit":"(?:Story|CometStory)"'
)


def _balanced_end(text: str, start: int, limit: int = 4_000_000) -> int:
    """Vị trí đóng ngoặc của object JSON bắt đầu tại `start` (-1 nếu không thấy)."""
    depth = 0
    in_str = False
    esc = False
    end = min(len(text), start + limit)
    j = start
    while j < end:
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    return -1


def _message_text(story: dict) -> Optional[str]:
    """Nội dung bài: tìm `message.text` trong chính bài (không lấy của story lồng)."""
    sections = story.get("comet_sections") or {}
    content = (sections.get("content") or {}).get("story") or {}
    inner = content.get("comet_sections") or {}
    msg = (inner.get("message") or {}).get("story", {}).get("message")
    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
        return msg["text"]
    direct = story.get("message")
    if isinstance(direct, dict) and isinstance(direct.get("text"), str) and direct["text"].strip():
        return direct["text"]
    if isinstance(direct, str) and direct.strip():
        return direct
    # Bài không có text (chỉ ảnh/video) hoặc bố cục khác: bắt message đầu tiên trong bài.
    for key in ("above_message", "message"):
        node = inner.get(key) or {}
        sub = (node.get("story") or {}).get("message") if isinstance(node, dict) else None
        if isinstance(sub, dict) and isinstance(sub.get("text"), str) and sub["text"].strip():
            return sub["text"]
    return None


def _story_origin(story: dict) -> dict:
    """Nguồn đăng: group / page / trang cá nhân + tên hiển thị."""
    to = story.get("to") or {}
    to_type = to.get("__typename") if isinstance(to, dict) else None
    source_type = {"Group": "group", "Page": "page", "User": "user"}.get(to_type or "", "user")
    name = to.get("name") if isinstance(to, dict) else None
    url = to.get("url") if isinstance(to, dict) else None
    fb_id = str(to.get("id")) if isinstance(to, dict) and to.get("id") else None
    if not name:
        for holder in (story.get("feedback") or {},):
            owning = holder.get("owning_profile") or {}
            if isinstance(owning, dict) and owning.get("name"):
                name = owning["name"]
                fb_id = fb_id or (str(owning.get("id")) if owning.get("id") else None)
                source_type = {"Page": "page", "Group": "group"}.get(
                    owning.get("__typename") or "", "user"
                )
                break
    if source_type == "group" and not name:
        grp = ((story.get("feedback") or {}).get("associated_group") or {})
        if grp.get("id"):
            fb_id = fb_id or str(grp["id"])
    return {
        "source_type": source_type,
        "source_fb_id": fb_id,
        "source_name": name,
        "source_url": url,
    }


def story_to_post(story: dict, origin_hint: Optional[dict] = None) -> Optional[dict]:
    """Một object story -> dict bài (link, nội dung, nguồn, ngày).

    `origin_hint` dùng khi quét feed của một Page: payload feed Page không có `to`,
    nên phải gán cứng nguồn = Page đang quét (loại `page`, tên/ID của Page).
    """
    if not isinstance(story, dict):
        return None
    post_id = story.get("post_id")
    url = story.get("permalink_url") or ""
    if not post_id and not url:
        return None
    if not url and post_id:
        to = story.get("to") or {}
        owner = to.get("id") if isinstance(to, dict) else None
        if owner:
            url = f"https://www.facebook.com/groups/{owner}/posts/{post_id}/"
    info = parse_post_link(url) if url else None
    origin = _story_origin(story)
    if origin_hint:
        # Quét feed của 1 Page: mọi bài lấy từ feed đó đều thuộc Page đang quét.
        # `source_fb_id` ưu tiên owner đọc được từ permalink (username/ID thật),
        # còn loại nguồn + tên hiển thị lấy từ Page người dùng nhập.
        origin = {
            "source_type": origin_hint.get("source_type") or origin["source_type"],
            "source_fb_id": origin.get("source_fb_id") or origin_hint.get("source_fb_id"),
            "source_name": origin_hint.get("source_name") or origin.get("source_name"),
            "source_url": origin_hint.get("source_url") or origin.get("source_url"),
        }
    if info:
        # Link dạng /groups/<id>/posts/<id> -> ưu tiên thông tin từ `to` cho đúng loại nguồn.
        info = {**info, "source_type": origin["source_type"] or info["source_type"]}
        if origin["source_fb_id"]:
            info["source_fb_id"] = origin["source_fb_id"]
    elif origin["source_fb_id"] and post_id:
        info = {
            "source_path": f"{origin['source_fb_id']}/posts/{post_id}",
            "source_type": origin["source_type"],
            "source_fb_id": origin["source_fb_id"],
            "fb_post_id": f"{origin['source_fb_id']}_posts_{post_id}",
        }
        url = url or f"https://www.facebook.com/{origin['source_fb_id']}/posts/{post_id}"
    if not info:
        return None
    actor = story.get("actors") or []
    if isinstance(actor, list):
        actor = actor[0] if actor else {}
    author = (actor or {}).get("name") if isinstance(actor, dict) else None
    author = _clean_name(author)
    ts = _ts_to_dt(story.get("creation_time"))
    if not ts:
        ts_section = ((story.get("comet_sections") or {}).get("timestamp") or {}).get("story") or {}
        ts = _ts_to_dt(ts_section.get("creation_time"))
    return {
        "url": url,
        "text": _message_text(story),
        "author": author,
        "posted_at": ts,
        "has_media": bool(story.get("attachments")),
        "origin": origin,
        "info": info,
    }


def parse_html_payload(html: str, origin_hint: Optional[dict] = None) -> list[dict]:
    """Bóc danh sách bài nhúng trong HTML / response GraphQL.

    Chỉ lấy story cấp cao nhất (`"__isFeedUnit":"Story"`), bỏ qua story lồng
    (attached_story / comment / share video) — chúng thường không có nội dung hoặc
    có nội dung của bài khác.
    """
    posts: dict[str, dict] = {}
    raw = html
    for m in _FEED_STORY.finditer(raw):
        start = raw.index("{", m.start())
        end = _balanced_end(raw, start)
        if end < 0:
            continue
        try:
            story = json.loads(raw[start : end + 1])
        except ValueError:
            continue
        post = story_to_post(story, origin_hint)
        if not post:
            continue
        path = post["info"]["source_path"]
        prev = posts.get(path)
        if prev is None:
            posts[path] = post
            continue
        # Cùng bài xuất hiện nhiều lần: giữ bản đầy đủ nhất.
        if not prev.get("text") and post.get("text"):
            prev["text"] = post["text"]
        if not prev.get("author") and post.get("author"):
            prev["author"] = post["author"]
        if not prev.get("posted_at") and post.get("posted_at"):
            prev["posted_at"] = post["posted_at"]
        if not prev.get("origin", {}).get("source_name") and post.get("origin", {}).get("source_name"):
            prev["origin"] = post["origin"]
    return list(posts.values())


# ------------------------------------------------------------------ DOM fallback
_JS_DOM = r"""
() => {
  const feed = document.querySelector('[role="feed"]');
  if (!feed) return [];
  const out = [];
  for (const kid of [...feed.children]) {
    const root = kid.querySelector('[aria-posinset]') || kid;
    const msg = root.querySelector('[data-ad-rendering-role="story_message"]');
    let text = msg ? (msg.innerText || '').trim() : '';
    if (!text) {
      // Bài chỉ có ảnh/video: lấy alt ảnh hoặc aria-label mô tả nội dung.
      const alts = [...root.querySelectorAll('img[alt]')]
        .map(i => (i.getAttribute('alt') || '').trim())
        .filter(a => a && !/^(Có thể là hình ảnh|Không có mô tả|Hình ảnh|Ảnh|Facebook)/i.test(a));
      const aria = [...root.querySelectorAll('[aria-label]')]
        .map(e => (e.getAttribute('aria-label') || '').trim())
        .filter(a => a && !/Bày tỏ|Bình luận|Chia sẻ|Hành động|Chỉ báo|Xem ai|Thích:|Video player|Trình phát/i.test(a));
      text = alts[0] || aria[0] || '';
    }
    text = text.replace(/… Xem thêm\s*$/, '').trim();
    const authorEl = root.querySelector('a[role="link"][href] strong, h3 a, h4 a');
    const timeEl = [...root.querySelectorAll('a, span')].find(
      e => /trước|[Ll]úc \d/.test(e.innerText || '')
    );
    const ownerLinks = [...root.querySelectorAll('a[href]')]
      .map(a => {
        const h = a.getAttribute('href') || '';
        const m = h.match(/facebook\.com\/(?:groups\/)?([A-Za-z0-9._-]{2,80})/);
        return m ? m[1] : null;
      })
      .filter(Boolean);
    out.push({
      index: out.length,
      text,
      ownerLinks,
      author: authorEl ? (authorEl.innerText || '').trim() : '',
      time: timeEl ? (timeEl.innerText || '').trim().slice(0, 80) : '',
      hasMedia: !!root.querySelector('img[src*="scontent"], video'),
    });
  }
  return out;
}
"""


def _dom_hits(items: list[dict], now: datetime, seen: set[str]) -> list[BrowserHit]:
    hits = []
    for item in items:
        key = item.get("key") or item.get("text") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        author = _clean_name((item.get("author") or "").split("·")[0]) or ""
        slug = re.sub(r"[^a-z0-9]+", "-", author.lower()).strip("-") or "khong-ro"
        digest = str(abs(hash(key)))[:14]
        hits.append(
            BrowserHit(
                fb_post_id=f"dom-{slug}-{digest}",
                source_path=f"dom/{slug}/{digest}",
                permalink=f"https://www.facebook.com/search/posts/?q={slug}",
                content=_clean_text(item.get("text")),
                author_name=_clean_name(author),
                source_type="unknown",
                source_fb_id=None,
                source_name=item.get("author") or None,
                posted_at=parse_date_text(item.get("time") or "", now),
                has_media=bool(item.get("hasMedia")),
                engine="browser-dom",
            )
        )
    return hits


def _fill_from_dom(hits: list[BrowserHit], items: list[dict]) -> None:
    """Bù nội dung cho bài thiếu chữ (bài chỉ có ảnh/video) từ dữ liệu DOM.

    Thứ tự bài trong payload và trên DOM thường lệch nhau (bài gợi ý, bài lặp), nên chỉ
    dùng DOM khi khớp CHÍNH XÁC tên nguồn/tác giả. Không khớp thì để trống — thà thiếu
    nội dung còn hơn gán nội dung của bài khác.
    """
    used: set[int] = set()

    def norm(value) -> str:
        return re.sub(r"[^a-z0-9]+", "", _strip_accents(value or ""))

    # Mỗi bài đích: tên nguồn + (tùy chọn) thời gian, khớp với tác giả bài trên DOM.
    for idx, hit in enumerate(hits):
        if hit.content:
            continue
        targets = {
            norm(x) for x in (hit.source_name, hit.author_name, hit.source_fb_id) if norm(x)
        }
        if not targets:
            continue
        for pos, item in enumerate(items):
            if pos in used or not item.get("text"):
                continue
            author = norm(item.get("author"))
            owners = {norm(x) for x in (item.get("ownerLinks") or [])}
            if author and author in targets or owners & targets:
                text = item["text"].strip()
                if text:
                    hit.content = text
                    used.add(pos)
                    break


def _strip_accents(text: str) -> str:
    import unicodedata

    text = (text or "").replace("đ", "d").replace("Đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()


# ------------------------------------------------------------------ main entry
def _payload_hits(
    payloads: list[str], seen: set[str], origin_hint: Optional[dict] = None
) -> list[BrowserHit]:
    hits: list[BrowserHit] = []
    for html in payloads:
        for story in parse_html_payload(html, origin_hint):
            path = story["info"]["source_path"]
            if path in seen:
                continue
            seen.add(path)
            hits.append(
                BrowserHit(
                    fb_post_id=story["info"]["fb_post_id"],
                    source_path=path,
                    permalink=story["url"],
                    content=story.get("text"),
                    author_name=story.get("author"),
                    source_type=story["info"]["source_type"],
                    source_fb_id=story["info"]["source_fb_id"],
                    source_name=(story.get("origin") or {}).get("source_name")
                    or story["info"]["source_fb_id"],
                    posted_at=story.get("posted_at"),
                    has_media=bool(story.get("has_media")),
                    engine="browser-html",
                )
            )
    return hits


def crawl(
    term: str,
    *,
    rng=None,
    scrolls: int = 30,
    max_posts: int = 0,
    headless: bool = True,
) -> BrowserCrawl:
    """Mở ô tìm kiếm Facebook bằng Chromium, cuộn và trả các bài đã thấy."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - phụ thuộc máy
        raise BrowserSearchError(
            "Chưa cài Playwright. Chạy: pip install playwright && python -m playwright install chromium"
        ) from exc

    res = BrowserCrawl()
    cookies = _cookies()
    now = datetime.now(VN_TZ)
    seen: set[str] = set()
    dom_seen: set[str] = set()

    url = SEARCH_URL.format(q=term.replace(" ", "%20"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            body = page.inner_text("body") or ""
            if "Log in to Facebook" in body or "Bạn phải đăng nhập" in body:
                raise BrowserSearchError(
                    "Facebook yêu cầu đăng nhập — cookies hết hạn, export lại facebook_cookies.json"
                )

            payloads = [page.content()]
            stopped = ""
            idle = 0
            for i in range(max(1, scrolls)):
                res.scrolls = i + 1
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(2200)
                html = page.content()
                before = len(seen)
                # chỉ bóc phần mới thêm ở cuối trang để khỏi duyệt lại 2MB HTML
                fresh_html = html[-2_000_000:] if i else html
                payloads.append(fresh_html)
                _ = _payload_hits([fresh_html], seen)
                if len(seen) == before:
                    idle += 1
                else:
                    idle = 0
                if idle >= 3:
                    stopped = f"hết bài mới sau {i + 1} lần cuộn"
                    break
                if max_posts and len(seen) >= max_posts:
                    stopped = f"đạt giới hạn {max_posts} bài"
                    break
                if rng is not None and getattr(rng, "start", None):
                    dates = [h.posted_at for h in res.hits if h.posted_at]
                    if dates and min(dates) < rng.start - timedelta(days=2):
                        stopped = "đã cuộn qua bài cũ hơn mốc đầu khoảng"
                        break
            res.stopped_reason = stopped or f"đã cuộn {res.scrolls} lần"
            res.hits = _payload_hits(payloads, set())

            dom_items = page.evaluate(_JS_DOM)
            if res.hits and dom_items:
                # Dữ liệu nhúng thiếu chữ với bài chỉ có ảnh/video -> bù nội dung từ DOM
                # theo đúng thứ tự bài trên trang.
                _fill_from_dom(res.hits, dom_items)
            if not res.hits:
                # HTML không có payload (FB đổi định dạng) -> đọc những bài đang hiển thị.
                res.hits = _dom_hits(dom_items, now, dom_seen)
                if res.hits:
                    res.errors.append(
                        "Trang không có dữ liệu nhúng — đã đọc bài hiển thị trên DOM "
                        "(không có link Facebook riêng cho từng bài)."
                    )
                else:
                    res.errors.append("Không đọc được bài nào từ trang tìm kiếm.")
        finally:
            browser.close()

    return res


# ------------------------------------------------------------------ feed Page
# Feed của Page không nằm trong HTML: Facebook nạp qua GraphQL
# (`ProfileCometTimelineFeedRefetchQuery`). Ta mở Page bằng Chromium rồi bắt các
# response GraphQL đó — dữ liệu đầy đủ như GraphQL nhưng không bị chặn 1357004.
PAGE_MAX_SCROLLS = 80
_FEED_STORY_MARKERS = ('"__isFeedUnit":"Story"', '"__isFeedUnit":"CometStory"')


def page_url(page: str) -> str:
    """Chuẩn hoá link/tên Page -> URL Facebook.

    Nhận: link đầy đủ, `facebook.com/<page>`, `@<page>`, `<page>`, hoặc ID số.
    """
    from urllib.parse import parse_qs, urlparse

    s = (page or "").strip()
    if not s:
        raise BrowserSearchError("Chưa nhập Page cần quét (link hoặc tên Page).")
    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        if "facebook.com" not in (u.netloc or ""):
            raise BrowserSearchError(f"Link Page phải là facebook.com/... (nhận '{page}')")
        if u.path.rstrip("/").endswith("profile.php"):
            pid = (parse_qs(u.query).get("id") or [""])[0]
            if pid:
                return f"https://www.facebook.com/profile.php?id={pid}"
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            raise BrowserSearchError(f"Link không phải trang Page: {page}")
        # Dán link bài viết vẫn quy về feed của Page chứa bài đó.
        return f"https://www.facebook.com/{unquote(parts[0])}"
    s = s.lstrip("@").strip("/")
    if not s:
        raise BrowserSearchError("Chưa nhập Page cần quét (link hoặc tên Page).")
    if s.isdigit():
        return f"https://www.facebook.com/profile.php?id={s}"
    return f"https://www.facebook.com/{s}"


def page_slug(page: str) -> str:
    """Username/ID dùng làm `source_fb_id` cho bài của Page."""
    from urllib.parse import parse_qs, urlparse

    s = (page or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        u = urlparse(s)
        if u.path.rstrip("/").endswith("profile.php"):
            return (parse_qs(u.query).get("id") or [""])[0]
        parts = [p for p in u.path.split("/") if p]
        return unquote(parts[0]) if parts else ""
    s = s.lstrip("@").strip("/")
    return s.split("/")[0]


def _page_name(page) -> str:
    """Tên Page đang mở (dùng cho `source_name` và tiêu đề kết quả)."""
    for sel in ('[role="main"] h1', "h1"):
        try:
            loc = page.locator(sel).first
            if loc.count():
                text = (loc.inner_text(timeout=2000) or "").strip()
                if text:
                    return _clean_name(text) or text
        except Exception:  # noqa: BLE001 - DOM khác nhau tùy bố cục FB
            continue
    title = (page.title() or "").split("|")[0].strip()
    title = re.sub(r"^\(\d+\)\s*", "", title)
    if title.lower() in ("facebook", ""):
        return ""
    return _clean_name(title) or ""


def _old_streak(hits: list[BrowserHit], cutoff: datetime) -> int:
    """Số bài LIÊN TIẾP (tính từ cuối danh sách) đã cũ hơn `cutoff`.

    Bài không rõ ngày thì cắt chuỗi — không dám kết luận đã cuộn qua mốc.
    Dùng chuỗi dài (8 bài) để bài ghim (pinned) cũ ở đầu feed không làm dừng sớm.
    """
    n = 0
    for hit in reversed(hits):
        if hit.posted_at and hit.posted_at < cutoff:
            n += 1
        else:
            break
    return n


def crawl_page(
    page_ref: str,
    *,
    rng=None,
    since: Optional[datetime] = None,
    scrolls: int = 30,
    max_posts: int = 0,
    headless: bool = True,
) -> BrowserCrawl:
    """Mở feed của một Page bằng Chromium, cuộn và trả các bài đã thấy.

    Bắt response GraphQL của feed Page (không đọc HTML vì feed Page được nạp bằng JS).
    `rng` có mốc đầu khoảng thì tự dừng sau khi đã cuộn qua bài cũ hơn mốc — đủ để lấy
    trọn bài trong ngày mà không cuộn hết lịch sử Page.

    `since` là **mốc quét lần trước** (bài mới nhất đã quét): feed Page xếp mới -> cũ,
    nên chỉ cần cuộn tới khi gặp 8 bài cũ hơn mốc là dừng — lần quét sau nhanh hơn
    nhiều so với cuộn lại cả ngày.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - phụ thuộc máy
        raise BrowserSearchError(
            "Chưa cài Playwright. Chạy: pip install playwright && python -m playwright install chromium"
        ) from exc

    res = BrowserCrawl()
    url = page_url(page_ref)
    slug = page_slug(page_ref)
    cookies = _cookies()
    bodies: list[str] = []
    hits: list[BrowserHit] = []
    seen: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()

        def _on_response(resp):
            if "/api/graphql" not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001 - response bị hủy/redirect
                return
            if body.startswith("for (;;);"):
                body = body[9:]
            if any(mark in body for mark in _FEED_STORY_MARKERS):
                bodies.append(body)

        page.on("response", _on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1500)
            text = page.inner_text("body") or ""
            low = text.lower()
            if "log in to facebook" in low or "bạn phải đăng nhập" in low:
                raise BrowserSearchError(
                    "Facebook yêu cầu đăng nhập — cookies hết hạn, export lại facebook_cookies.json"
                )
            if (
                "bạn hiện không xem được nội dung này" in low
                or "this content isn't available" in low
                or "nội dung này hiện không hiển thị" in low
            ):
                raise BrowserSearchError(
                    f"Facebook không cho xem Page '{page_ref}' bằng tài khoản hiện tại "
                    "(Page đã đổi tên/xóa hoặc giới hạn người xem)."
                )

            res.page_name = _page_name(page)
            hint = {
                "source_type": "page",
                "source_fb_id": slug,
                "source_name": res.page_name or slug,
                "source_url": url,
            }
            try:
                page.wait_for_selector('[role="feed"]', timeout=15000)
            except Exception:  # noqa: BLE001 - một số Page không có role=feed
                pass
            page.wait_for_timeout(2500)

            # Bài mới nhất của Page thường được server-render thẳng trong HTML,
            # không nằm trong response GraphQL -> bóc HTML trước để không bỏ sót.
            hits.extend(_payload_hits([page.content()], seen, hint))

            stopped = ""
            idle = 0
            parsed_bodies = 0
            # Mốc dừng = mốc xa hơn giữa "đầu khoảng ngày" và "mốc quét lần trước".
            cutoff = None
            if rng is not None and getattr(rng, "start", None):
                cutoff = rng.start
            if since is not None and (cutoff is None or since > cutoff):
                cutoff = since
            res.cutoff = cutoff
            for i in range(max(1, scrolls)):
                res.scrolls = i + 1
                page.mouse.wheel(0, 7000)
                page.wait_for_timeout(2200)

                # Bài mới nhất có thể chỉ nằm trong HTML (server-render), không có
                # response GraphQL -> bóc cả HTML mỗi lần cuộn, nếu không sẽ dừng sớm
                # và tưởng "hết bài" dù feed còn bài.
                html_added = _payload_hits([page.content()], seen, hint)
                hits.extend(html_added)

                fresh = bodies[parsed_bodies:]
                parsed_bodies = len(bodies)
                added = _payload_hits(fresh, seen, hint)
                hits.extend(added)
                idle = 0 if (added or html_added) else idle + 1

                if idle >= 3:
                    stopped = f"hết bài mới sau {i + 1} lần cuộn"
                    res.complete = True
                    break
                if max_posts and len(hits) >= max_posts:
                    stopped = f"đạt giới hạn {max_posts} bài"
                    break
                if cutoff is not None and _old_streak(hits, cutoff) >= 8:
                    # Feed Page xếp mới -> cũ: 8 bài liên tiếp cũ hơn mốc nghĩa là
                    # phần còn lại cũng cũ hơn -> không cần cuộn tiếp.
                    if since is not None and cutoff >= since:
                        stopped = "đã cuộn qua mốc quét lần trước"
                    else:
                        stopped = "đã cuộn qua bài cũ hơn mốc đầu khoảng"
                    res.complete = True
                    break

            res.stopped_reason = stopped or f"đã cuộn {res.scrolls} lần"
            if not hits:
                # Một số Page nhúng sẵn bài trong HTML thay vì gọi GraphQL.
                hits = _payload_hits([page.content()], set(), hint)
            if max_posts and len(hits) > max_posts:
                hits = hits[:max_posts]
            if not res.page_name:
                res.page_name = next((h.author_name for h in hits if h.author_name), "") or ""
            if hits:
                # Tên tác giả lấy từ payload đáng tin hơn <h1> trên trang (có thể là
                # tiêu đề panel "Thông báo" của Facebook).
                author = next((h.author_name for h in hits if h.author_name), "")
                if author:
                    res.page_name = author
            if res.page_name:
                for h in hits:
                    if h.source_type == "page" and (not h.source_name or h.source_name == slug):
                        h.source_name = res.page_name
            res.hits = hits
            if not res.hits:
                if "chưa có bài viết" in low or "no posts" in low:
                    res.stopped_reason = "Page chưa có bài viết nào"
                else:
                    res.errors.append(
                        f"Không đọc được bài nào từ Page '{page_ref}'. "
                        "Kiểm tra lại link Page hoặc mở Page bằng trình duyệt để xem có bị giới hạn không."
                    )
        finally:
            browser.close()

    return res


_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\u2060\ufeff]")

def _clean_text(text: Optional[str]) -> Optional[str]:
    """Bỏ ký tự zero-width/không in được trong nội dung bài."""
    if not isinstance(text, str):
        return None
    cleaned = _INVISIBLE.sub("", text).strip()
    return cleaned or None


def _clean_name(name) -> Optional[str]:
    """Bỏ ký tự vô hình/zero-width mà Facebook chèn vào tên tác giả."""
    if not isinstance(name, str):
        return None
    cleaned = " ".join(_INVISIBLE.sub("", name).split())
    return cleaned or None


def _make_title(content: Optional[str]) -> Optional[str]:
    """Tiêu đề ngắn = dòng đầu tiên có chữ của nội dung bài."""
    for line in (content or "").split("\n"):
        clean = line.strip(" \t-–—·|")
        if len(clean) >= 3:
            return clean[:160]
    return None


def post_row(hit: BrowserHit, *, keyword_id: Optional[int]) -> dict:
    """Chuyển BrowserHit sang dict cho `db.insert_browser_post`."""
    return {
        "fb_post_id": hit.fb_post_id,
        "source_path": hit.source_path,
        "permalink": hit.permalink,
        "content": hit.content,
        "title": _make_title(hit.content),
        "author_name": hit.author_name,
        "source_type": hit.source_type,
        "source_fb_id": hit.source_fb_id,
        "source_name": hit.source_name,
        "source_url": hit.permalink,
        "posted_at": hit.posted_at,
        "has_media": hit.has_media,
        "keyword_id": keyword_id,
        "fetched_by": f"browser:{hit.engine}",
    }


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "BrowserCrawl",
    "BrowserHit",
    "BrowserSearchError",
    "SEARCH_URL",
    "available",
    "crawl",
    "crawl_page",
    "page_slug",
    "page_url",
    "parse_date_text",
    "parse_html_payload",
    "parse_post_link",
    "post_row",
]
