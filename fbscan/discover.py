
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Iterable

from . import config, console, db

console.patch_console()

# "Nhóm của bạn" (ordering=viewer_added) mới thật sự chỉ liệt kê group đã tham gia.
# Tab "Khám phá" nằm ngay cạnh nên cần URL riêng, không dựa vào trang mặc định.
JOINS_URL = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"
HOME_URL = "https://www.facebook.com/"
# Trang có link group nhưng không phải danh sách nhóm của bạn (chỉ để tham khảo).
NOTE_URL = "https://www.facebook.com/groups/joins/"

# URL con của FB không phải trang group thật.
_SKIP_SLUGS = {
    "joins",
    "feed",
    "discover",
    "create",
    "categories",
    "search",
    "your_groups",
    "invites",
    "membership_request",
    "manage",
    "permalink",
    "browse",
    "invite",
    "notifications",
    "learn",
    "policies",
    "help",
}
_CHECKPOINT_HINTS = ("two_step_verification", "checkpoint", "/login", "recover")

_JS_COLLECT = """
() => {
    const out = [];
    for (const a of document.querySelectorAll("a[href*='/groups/']")) {
        const name = (a.innerText || "").trim().split("\\n")[0].trim();
        out.push([a.getAttribute("href") || a.href || "", name]);
    }
    return out;
}
"""

_JS_COLLECT_NAMES = """
() => {
    // Link bao quanh thẻ <h3>/<h2> của tên nhóm là link sạch nhất (trong trường hợp này innerText
    // chỉ là tên nhóm, không lẫn "Chưa đọc ... có một bài viết mới").
    const out = [];
    for (const h of document.querySelectorAll("h2 a[href*='/groups/'], h3 a[href*='/groups/']")) {
        out.push([h.getAttribute("href") || h.href || "", (h.innerText || "").trim().split("\\n")[0].trim()]);
    }
    return out;
}
"""

_JS_SCROLL = """
() => {
    const se = document.scrollingElement || document.documentElement;
    const before = se.scrollHeight;
    se.scrollTop = se.scrollHeight;
    window.scrollTo(0, before);
    // FB đôi khi dùng div nội bộ làm vùng cuộn chính
    for (const d of document.querySelectorAll("div")) {
        if (d.clientHeight > 400 && d.scrollHeight > d.clientHeight + 200) {
            d.scrollTop = d.scrollHeight;
        }
    }
    return [before, se.scrollHeight, document.querySelectorAll("a[href*='/groups/']").length];
}
"""


def _is_logged_out_url(url: str) -> bool:
    """True khi đang ở trang login / 2FA / checkpoint thật (không chỉ vì URL chứa chuỗi)."""
    u = url.lower()
    if not u.startswith("http"):
        return False
    if "facebook.com" not in u:
        return True
    if "/checkpoint/" in u or "/two_step_verification" in u or "/recover" in u:
        return True
    # "/login" (chứ không phải "/login/xxx") mới là trang đăng nhập thật.
    path = "/" + u.split("facebook.com", 1)[-1].split("?", 1)[0].lstrip("/")
    return path.rstrip("/") == "/login" or path.startswith("/login.php")


def _safe_goto(page, url: str, *, timeout: int = 90_000, tries: int = 3) -> bool:
    """goto() không ném lỗi khi FB tự redirect (2FA/checkpoint) giữa chừng."""
    for attempt in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as exc:  # PlaywrightError: "interrupted by another navigation"
            msg = str(exc).splitlines()[0]
            print(f"  ! goto lần {attempt} bị ngắt: {msg[:160]}", flush=True)
            if "ERR_ABORTED" in msg or "interrupted" in msg.lower():
                # FB đã tự chuyển sang trang 2FA/checkpoint -> để người dùng xử lý
                time.sleep(3)
                return False
            time.sleep(2)
    return False


def _needs_login(page) -> bool:
    if _is_logged_out_url(page.url):
        return True
    try:
        if page.locator('input[name="email"]').count() > 0 and page.locator(
            'input[name="pass"]'
        ).count() > 0:
            return True
        if page.locator('[data-testid="royal_login_form"]').count() > 0:
            return True
        # Banner "Bạn phải đăng nhập để tiếp tục" / "You must log in to continue"
        if page.get_by_text("Bạn phải đăng nhập", exact=False).count() > 0:
            return True
        if page.get_by_text("You must log in to continue", exact=False).count() > 0:
            return True
    except Exception:
        pass
    return False


def _looks_logged_in(page) -> bool:
    """Đã vào được trang FB thật (không phải login/2FA/checkpoint/trang đang tải)."""
    url = page.url
    if not url.startswith("http") or "facebook.com" not in url:
        return False
    if _is_logged_out_url(url) or _needs_login(page):
        return False
    try:
        title = page.title()
        if title.startswith("Loading"):
            return False
        body = page.inner_text("body")
        # Trang login chỉ có ô email/mật khẩu + "Đăng ký"
        if "Mật khẩu" in body and "Đăng nhập" in body and len(body) < 500:
            return False
        # Trang đã render xong: có dữ liệu link group hoặc thanh menu chính.
        if page.eval_on_selector_all("a[href*='/groups/']", "els => els.length") > 0:
            return True
        if page.locator('[role="navigation"]').count() > 0:
            return True
        return len(body) > 200
    except Exception:
        return False


def _wait_for_login(page, timeout_sec: int = 600) -> bool:
    """Chờ người dùng đăng nhập (kể cả 2FA) trong cửa sổ trình duyệt.

    Trả True khi đã vào được trang của FB (không còn trang login/2FA).
    """
    print(
        "\n" + "=" * 62 + "\n"
        "CẦN ĐĂNG NHẬP FACEBOOK (có hỗ trợ 2 lớp xác thực)\n"
        "  1. Trong cửa sổ Chromium vừa mở: nhập mật khẩu, rồi mã 2FA.\n"
        "  2. Nếu FB hỏi 'Thiết bị lạ' / gửi mã về điện thoại -> xác nhận luôn.\n"
        "  3. Khi đã vào được News Feed / trang group, tool tự tiếp tục.\n"
        "     (Nếu bị kẹt ở trang 2FA quá lâu, tool vẫn thử lại tự động.)\n"
        f"  Chờ tối đa {timeout_sec // 60} phút — Ctrl+C để hủy.\n"
        + "=" * 62 + "\n",
        flush=True,
    )

    deadline = time.time() + timeout_sec
    last_note = ""
    while time.time() < deadline:
        time.sleep(3)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception as exc:
            # Trang đang điều hướng liên tục (FB tự redirect) -> thử tiếp
            if "closed" in str(exc).lower():
                break
            continue
        url = page.url
        if _looks_logged_in(page):
            print(f"  OK: đã đăng nhập ({url[:80]})", flush=True)
            return True
        note = "2FA/checkpoint" if "two_step_verification" in url or "checkpoint" in url else "login"
        if note != last_note:
            print(f"  ... đang chờ xử lý {note}: {url[:90]}", flush=True)
            last_note = note
    print("  X: hết thời gian chờ đăng nhập.", flush=True)
    return False


def _clean_name(name: str) -> str:
    """Bỏ tiền tố/số đếm thông báo mà FB chèn vào innerText của link nhóm."""
    name = (name or "").strip().splitlines()[0].strip() if name else ""
    if not name:
        return ""
    # "Chưa đọcKHU CHUNG CƯ ... có một bài viết mới.1 ngày" -> "KHU CHUNG CƯ ..."
    name = re.sub(r"^(Chưa đọc|Unread)\s*", "", name).strip()
    for pat in (
        r"\.?\s*\d+ bài viết mới\.?$",   # "có 1 bài viết mới" (link "Xem nhóm" đi kèm)
        r"\.?\s*\d+ new posts?\.?$",
        r"\.?\s*\d+ (ngày|giờ|phút|tuần|tháng|năm)$",
        r"\.?\s*\d+ (days?|hours?|minutes?|weeks?|months?|years?) ago$",
        r"\.?\s*Có \d+ dấu$",           # "Có 3 dấu" (bài chưa đọc)
        r"^(Xem nhóm|View group)$",
    ):
        name = re.sub(pat, "", name, flags=re.I).strip()
    # "...có một bài viết mới" đứng ngay trước "1 ngày" vừa bị cắt
    name = re.sub(r"\s*có\s+m\S*\s+bài viết mới", "", name, flags=re.I).strip()  # "…có một bài viết mới"
    name = re.sub(r"[.…\s]*\d+ (ngày|giờ|phút|tuần|tháng|năm)$", "", name, flags=re.I).strip()
    name = re.sub(r"[.…\s]+$", "", name).strip()
    return name


def _collect_groups(page, found: dict[str, dict]) -> int:
    """Đọc link group trên trang hiện tại, thêm vào `found`. Trả số group mới."""
    added = 0
    try:
        rows = page.evaluate(_JS_COLLECT)
        # Tên lấy từ link bao quanh <h2>/<h3> chính xác hơn innerText của mọi link.
        h_rows = page.evaluate(_JS_COLLECT_NAMES)
    except Exception as exc:  # trang tự điều hướng giữa lúc chạy script
        print(
            f"  ! không đọc được DOM ({str(exc).splitlines()[0][:120]})", flush=True
        )
        return 0
    named_for: dict[str, str] = {}
    for href, name in h_rows:
        m = re.search(r"/groups/([A-Za-z0-9._-]+)", href or "")
        if m and _clean_name(name):
            named_for[m.group(1)] = _clean_name(name)
    for href, name in rows + h_rows:
        m = re.search(r"/groups/([A-Za-z0-9._-]+)", href or "")
        if not m:
            continue
        slug = m.group(1)
        if slug.lower() in _SKIP_SLUGS:
            continue
        item = found.get(slug)
        if item is None:
            found[slug] = {
                "slug": slug,
                "group_url": f"https://www.facebook.com/groups/{slug}",
            }
            item = found[slug]
            added += 1
        name = named_for.get(slug) or _clean_name(name)
        if name and not item.get("name"):
            item["name"] = name
    return added


def _ensure_logged_in(page, login_timeout: int) -> None:
    """Chờ người dùng đăng nhập nếu phiên đã hết hạn / bị checkpoint."""
    if _wait_for_login(page, login_timeout):
        return
    raise SystemExit(
        "Chưa đăng nhập xong. Chạy lại lệnh sau khi đăng nhập thành công,\n"
        "hoặc thêm group thủ công: python -m fbscan.discover --source manual --group <id|slug>"
    )


def discover_with_playwright(
    *,
    user_data_dir: Path,
    headless: bool = False,
    max_scroll: int = 40,
    scroll_wait: float = 1.6,
    login_timeout: int = 600,
) -> list[dict]:
    """Mở trình duyệt (profile lưu ở user_data_dir), trả danh sách group."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Thiếu Playwright. Cài:\n  pip install playwright\n  playwright install chromium"
        ) from exc

    found: dict[str, dict] = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            viewport={"width": 1400, "height": 900},
            locale="vi-VN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Mở facebook.com trước: nếu phiên hết hạn/checkpoint thì FB redirect ngay
        # và người dùng đăng nhập trên trang nhà, không bị mất trang đích.
        _safe_goto(page, HOME_URL)
        time.sleep(3)
        if not _looks_logged_in(page):
            _ensure_logged_in(page, login_timeout)

        if not _safe_goto(page, JOINS_URL):
            time.sleep(3)
        if _needs_login(page):
            _ensure_logged_in(page, login_timeout)

        # FB render danh sách bằng JS sau khi DOM xong -> chờ link group xuất hiện.
        try:
            page.wait_for_selector("a[href*='/groups/']", timeout=30_000)
        except Exception:
            print("  ! chưa thấy link group nào sau 30s, vẫn thử cuộn...", flush=True)
        time.sleep(2)
        if _needs_login(page):
            _ensure_logged_in(page, login_timeout)

        last_height = -1
        same_for = 0
        for i in range(max_scroll):
            added = _collect_groups(page, found)
            try:
                # mouse.wheel KHÔNG làm FB nạp thêm group -> cuộn bằng JS.
                before, height, n_links = page.evaluate(_JS_SCROLL)
            except Exception as exc:  # trang tự điều hướng giữa lúc chạy script
                print(
                    f"  ! trang đổi giữa chừng ({str(exc).splitlines()[0][:120]}), dừng cuộn",
                    flush=True,
                )
                break
            print(
                f"  scroll {i + 1}/{max_scroll}: {len(found)} group (+{added}), height={height}",
                flush=True,
            )
            if height <= before and added == 0:
                same_for += 1
                if same_for >= 3 and i >= 2:
                    break
            else:
                same_for = 0
            last_height = height
            time.sleep(scroll_wait)
            _collect_groups(page, found)

        # Lấy tên group từ DOM (link text) — nếu không có thì để trống.
        _collect_groups(page, found)

        ctx.close()

    return list(found.values())


def parse_manual(lines: Iterable[str]) -> list[dict]:
    """Mỗi dòng: id hoặc slug hoặc link group (có thể kèm |Tên)."""
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = None
        if "|" in line:
            line, name = [p.strip() for p in line.split("|", 1)]
        m = re.search(r"/groups/([A-Za-z0-9._-]+)", line)
        slug = m.group(1) if m else line.strip("/")
        out.append(
            {
                "slug": slug,
                "name": name,
                "group_url": f"https://www.facebook.com/groups/{slug}",
            }
        )
    return out


def save_groups(items: list[dict]) -> int:
    saved = 0
    with db.get_conn() as conn:
        for it in items:
            slug = it.get("slug", "")
            fb_id = slug if slug.isdigit() else slug
            if not fb_id:
                continue
            db.upsert_group(
                conn,
                fb_id,
                slug=slug if not slug.isdigit() else None,
                name=it.get("name"),
                group_url=it.get("group_url"),
            )
            saved += 1
    return saved


def resolve_names() -> None:
    """Dùng facebook-gql-scraper để lấy tên/id chính xác cho các group chỉ có slug."""
    from . import context as fbctx

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, fb_group_id, slug FROM scan_groups WHERE name IS NULL"
        ).fetchall()

    if not rows:
        return
    ctx = fbctx.make_context()
    for row in rows:
        ref = row["fb_group_id"]
        try:
            grp = fbctx.resolve_group(ctx, ref)
            with db.get_conn() as conn:
                conn.execute(
                    """
                    UPDATE scan_groups SET fb_group_id=%s, slug=%s, name=%s, privacy=%s,
                        member_count_txt=%s, group_url=%s, avatar_path=%s
                    WHERE id=%s
                    """,
                    (
                        str(grp.id),
                        ref if not str(ref).isdigit() else row["slug"],
                        getattr(grp, "name", None) or f"Group {grp.id}",
                        getattr(grp, "privacy", None),
                        getattr(grp, "member_count", None),
                        getattr(grp, "url", None),
                        getattr(grp, "profile_picture_url", None),
                        row["id"],
                    ),
                )
            print(f"  OK: {ref} -> {getattr(grp, 'name', '?')} ({grp.id})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  X {ref}: {type(exc).__name__}: {exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phát hiện group đã tham gia")
    ap.add_argument("--source", choices=["playwright", "manual"], default="playwright")
    ap.add_argument("--file", help="file text chứa id/slug/link (với --source manual)")
    ap.add_argument("--group", action="append", default=[], help="id/slug/link thêm tay")
    ap.add_argument("--profile", default=str(config.ROOT / ".fb-browser-profile"))
    ap.add_argument("--max-scroll", type=int, default=40, help="số lần cuộn trang (mặc định 40)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument(
        "--login-timeout",
        type=int,
        default=600,
        help="số giây chờ đăng nhập/2FA (mặc định 600)",
    )
    ap.add_argument("--no-resolve", action="store_true", help="không gọi GraphQL lấy tên")
    args = ap.parse_args(argv)

    if args.source == "manual":
        lines = list(args.group)
        if args.file:
            lines += Path(args.file).read_text(encoding="utf-8").splitlines()
        items = parse_manual(lines)
    else:
        try:
            items = discover_with_playwright(
                user_data_dir=Path(args.profile),
                headless=args.headless,
                login_timeout=args.login_timeout,
                max_scroll=args.max_scroll,
            )
        except SystemExit as exc:
            if exc.code not in (0, None) and isinstance(exc.code, str):
                print(f"\nX: {exc.code}", file=sys.stderr)
                return 2
            raise

    n = save_groups(items)
    print(f"Đã lưu {n} group vào scan_groups.")
    if n == 0:
        print(
            "\nKhông tìm thấy group nào.\n"
            "  - Kiểm tra đã đăng nhập đúng tài khoản có group chưa\n"
            "    (mở chromium profile: python -m fbscan.discover --source playwright và xem cửa sổ).\n"
            "  - Nếu cửa sổ dừng ở facebook.com/checkpoint/... hoặc trang 'Thiết bị lạ':\n"
            "    xác nhận xong trong cửa sổ đó rồi chạy lại (đừng đóng cửa sổ ngay).\n"
            "  - Thử tăng số lần cuộn: --max-scroll 80\n"
            "  - Hoặc thêm tay: python -m fbscan.discover --source manual --group <id|slug>",
            file=sys.stderr,
        )
        return 1 if args.source == "playwright" else 0
    if not args.no_resolve:
        resolve_names()
    return 0


if __name__ == "__main__":
    sys.exit(main())
