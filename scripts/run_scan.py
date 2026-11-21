"""Chạy quét bài viết group / theo từ khóa.

Ví dụ:
    python scripts/run_scan.py --all
    python scripts/run_scan.py --group 123456 --group kynangbanhang --max 50
    python scripts/run_scan.py --db-group 3 --days 7
    python scripts/run_scan.py --all --days 3 --reset-cursor
    python scripts/run_scan.py --all --no-media
    python scripts/run_scan.py --keyword "tuyển dụng" --days 7   # ô tìm kiếm FB

Thực thi yêu cầu do API đặt (POST /api/scan, cả theo group lẫn theo từ khóa):
    python scripts/run_scan.py --pending                 # làm hết hàng đợi rồi thoát
    python scripts/run_scan.py --pending --watch 60      # chạy mãi, 60s kiểm tra 1 lần
"""

from __future__ import annotations

import argparse
import datetime as _dt
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbscan import config, console, context as fbctx, db, dtrange, scanner  # noqa: E402
from fbscan import browser_search, search_scan  # noqa: E402

console.patch_console()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Quét bài viết Facebook group")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--all", action="store_true", help="quét mọi group is_active")
    src.add_argument("--group", action="append", default=[], help="id/slug/link (lặp lại được)")
    src.add_argument("--db-group", type=int, action="append", default=[], help="id trong bảng scan_groups")
    src.add_argument("--pending", action="store_true", help="thực thi yêu cầu trong scan_requests")
    src.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="quét ô tìm kiếm Facebook theo từ khóa (lặp lại được)",
    )
    src.add_argument(
        "--page",
        action="append",
        default=[],
        help="quét feed của Page theo link/tên Page (lặp lại được), vd --page thanhnien",
    )
    ap.add_argument("--max", type=int, default=None, help="số bài tối đa mỗi group/từ khóa")
    time_grp = ap.add_mutually_exclusive_group()
    time_grp.add_argument("--days", type=int, default=None, help="chỉ lấy bài trong N ngày gần nhất")
    time_grp.add_argument("--year", type=int, default=None, metavar="YYYY", help="chỉ lấy bài trong 1 năm")
    time_grp.add_argument(
        "--from", dest="date_from", default=None, metavar="YYYY-MM-DD", help="từ ngày (bao gồm)"
    )
    ap.add_argument("--to", dest="date_to", default=None, metavar="YYYY-MM-DD", help="đến ngày (bao gồm)")
    ap.add_argument(
        "--lookback",
        type=int,
        default=None,
        help="với --keyword/--year: đọc tối đa N bài kết quả tìm kiếm (mặc định 1000)",
    )
    ap.add_argument(
        "--full-scan",
        dest="full_page",
        action="store_true",
        help="với --page: quét lại toàn bộ khoảng đã chọn, bỏ qua mốc quét lần trước",
    )
    ap.add_argument(
        "--page-cursors",
        action="store_true",
        help="liệt kê mốc quét của các Page rồi thoát",
    )
    ap.add_argument(
        "--forget-page",
        metavar="PAGE",
        help="xoá mốc quét của Page (lần sau quét lại từ đầu) rồi thoát",
    )
    ap.add_argument("--reset-cursor", action="store_true", help="quét lại từ đầu")
    ap.add_argument("--no-media", action="store_true", help="không tải ảnh")
    ap.add_argument(
        "--keyword-all",
        action="store_true",
        help="với --keyword: lấy MỌI bài ô tìm kiếm trả về (group + Page + trang cá nhân)",
    )
    ap.add_argument(
        "--sort",
        choices=["recent", "relevance"],
        default="recent",
        help="với --keyword: recent = bài mới nhất trước (cần cho --year/--from), relevance = mặc định FB",
    )
    ap.add_argument(
        "--match",
        choices=["strict", "normal", "loose", "off"],
        default="strict",
        help=(
            "với --keyword: chỉ lưu bài có từ khóa trong tiêu đề/nội dung. "
            "strict (mặc định) = đủ mọi từ, loose = đủ 1 từ, off = không kiểm tra"
        ),
    )
    ap.add_argument(
        "--engine",
        choices=["auto", "graphql", "browser"],
        default="auto",
        help=(
            "với --keyword: auto = GraphQL, bị Facebook chặn (error 1357004) thì đọc trang "
            "search bằng browser; browser = luôn đọc trang; graphql = chỉ GraphQL"
        ),
    )
    ap.add_argument(
        "--watch",
        type=int,
        default=0,
        metavar="SEC",
        help="chỉ dùng với --pending: chạy mãi, mỗi SEC giây kiểm tra hàng đợi",
    )
    return ap.parse_args(argv)


def _ctx():
    try:
        return fbctx.make_context(quiet=False)
    except fbctx.LoginRequired as exc:
        # Không có phiên GraphQL vẫn có thể quét từ khóa bằng browser (engine auto/browser).
        print(f"CANH BAO: {exc} -> sẽ thử đọc trang tìm kiếm bằng browser", file=sys.stderr)
        return None


def _run_groups(ctx, refs: list[str], args, *, trigger: str = "cli") -> int:
    groups = []
    for ref in refs:
        try:
            groups.append(fbctx.resolve_group(ctx, ref))
            print(f"OK: resolve {ref} -> {getattr(groups[-1], 'name', '?')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"X bỏ qua {ref}: {type(exc).__name__}: {exc}", flush=True)

    if not groups:
        print("Không resolve được group nào.", file=sys.stderr)
        return 1

    results = scanner.scan_many(
        ctx,
        groups,
        trigger=trigger,
        max_posts=args.max,
        max_age_days=args.days,
        reset_cursor=args.reset_cursor,
        download_media=False if args.no_media else None,
    )

    total_new = sum(r.new for r in results)
    errors = sum(1 for r in results if r.errors)
    print(
        f"\nTổng: {len(results)} group, {sum(r.found for r in results)} bài, "
        f"{total_new} bài mới, {errors} group lỗi."
    )
    return 0 if errors == 0 else 3


# ------------------------------------------------------------------ pending
def _cli_range(args) -> dtrange.DateRange:
    """Khoảng ngày từ cờ CLI (--year / --from / --to, hoặc --days N ngày gần nhất)."""
    if args.year is not None or args.date_from or args.date_to:
        return dtrange.build_range(
            from_year=args.year, from_date=args.date_from, to_date=args.date_to
        )
    if getattr(args, "days", None):
        # "N ngày gần nhất" tính theo ngày Việt Nam, bao gồm hôm nay.
        today = _dt.date.today()
        return dtrange.build_range(
            from_date=(today - _dt.timedelta(days=args.days - 1)).isoformat(),
            to_date=today.isoformat(),
        )
    return dtrange.DateRange()


def _range_text(req: dict) -> str:
    if req.get("date_from") or req.get("date_to"):
        return dtrange.DateRange(req.get("date_from"), req.get("date_to")).describe()
    return "toàn bộ" if not req.get("days") else f"{req['days']} ngày"


def _run_request(ctx, req: dict) -> str:
    """Thực thi 1 yêu cầu trong scan_requests (ô tìm kiếm: group hoặc từ khóa)."""
    gid = req.get("group_id")
    with db.get_conn() as conn:
        term = None
        kw_id = req.get("keyword_id")
        if kw_id:
            kw_row = db.get_keyword(conn, req["keyword_id"])
            if not kw_row:
                print(f"X yêu cầu #{req['id']}: không thấy từ khóa {req['keyword_id']}", flush=True)
                return "error"
            term = kw_row["term"]
        else:
            grp_row = db.get_group(conn, gid) if gid else None
            if not grp_row:
                print(f"X yêu cầu #{req['id']}: không thấy group {gid}", flush=True)
                return "error"
            term = grp_row["name"] or grp_row["fb_group_id"]

    if ctx is None and not kw_id:
        print(f"X yêu cầu #{req['id']}: quét feed group cần phiên GraphQL (thiếu cookies)", flush=True)
        return "error"

    print(
        f"\n>>> Yêu cầu #{req['id']} — ô tìm kiếm \"{term}\": {_range_text(req)}, "
        f"tối đa {req['max_posts'] or 'không giới hạn'} bài"
        f"{' (lấy mọi nguồn)' if req.get('search_all') else ' (chỉ bài group)'}"
        f"{' + loại trừ ' + str(len(req.get('exclusions') or [])) + ' bài' if req.get('exclusions') else ''}",
        flush=True,
    )

    res = search_scan.run_request(ctx, {**req, "_term": term})
    return "error" if res["status"] == "error" else "done"


def _run_keyword(ctx, term: str, args, *, trigger: str = "cli") -> int:
    """Quét 1 lần theo từ khóa từ dòng lệnh (--keyword)."""
    with db.get_conn() as conn:
        kw = db.upsert_keyword(conn, term)

    rng = _cli_range(args)
    res = search_scan.run_request(
        ctx,
        {
            "keyword_id": kw["id"],
            "group_id": None,
            "requested_by": trigger,
            "date_from": rng.start,
            "date_to": rng.end,
            "max_posts": args.max or 0,
            "lookback": args.lookback,
            "download_media": False if args.no_media else None,
            "search_all": args.keyword_all,
            "keyword_match": args.match,
            "sort": args.sort,
            "engine": args.engine,
            "_term": kw["term"],
        },
    )
    print(
        f"\nTổng: \"{kw['term']}\" ({rng.describe()}) — FB trả {res['found']} bài, "
        f"trong khoảng {res['in_range']}, mới {res['new']}, đã có {res['existing']}, "
        f"bỏ qua {res['skipped']}, lỗi {res['failed']}."
    )
    return 0 if res["status"] != "error" else 3


def _run_page(ctx, page_ref: str, args, *, trigger: str = "cli") -> int:
    """Quét feed của 1 Page (đọc bằng browser, không cần phiên GraphQL)."""
    rng = _cli_range(args)
    resume = not (getattr(args, "full_page", False) or getattr(args, "reset_cursor", False))
    cur = search_scan.page_cursor(page_ref) if resume else None
    if cur:
        print(f"   mốc quét lần trước ({cur['last_scan_at'][:16]}): bài mới nhất {cur['last_posted_text']}")
    res = search_scan.review_search(
        ctx,
        page_ref,
        page=page_ref,
        rng=rng,
        lookback=args.lookback,
        max_posts=args.max or 0,
        engine="browser",
        resume=resume,
        download_media=False if args.no_media else True,
        created_by=trigger,
    )
    print(
        f"\nTổng: Page \"{res.get('page_name') or page_ref}\" ({rng.describe()}) — "
        f"FB trả {res['found']} bài, trong khoảng {res['in_range']}, mới {res['new']}, "
        f"đã có {res['existing']}, bỏ qua {res['skipped']}, lỗi {res['failed']}."
    )
    if res.get("cursor_updated"):
        print(f"   mốc quét mới: {res.get('newest_posted_text') or res.get('cursor', {}).get('last_posted_text', '')}")
    elif res.get("since") and not res.get("complete"):
        print("   chưa cập nhật mốc (lượt quét dừng sớm) — lần sau sẽ quét lại phần còn thiếu.")
    return 0 if not res.get("rejected") else 3


def _print_page_cursors() -> int:
    rows = _rows_page_cursors()
    if not rows:
        print("(chưa có mốc quét Page nào)")
        return 0
    print(f"{'page':<28}  {'last_scan':<16}  {'newest_post':<16}  {'found':>5}  {'new':>4}")
    for r in rows:
        print(
            f"{(r['page_name'] or r['source_key'])[:28]:<28}  "
            f"{r['last_scan_at'].strftime('%Y-%m-%d %H:%M') if r['last_scan_at'] else '-':<16}  "
            f"{search_scan._fmt_vn(r['last_posted_at'])[:16]:<16}  "
            f"{r['last_found']:>5}  {r['last_new']:>4}"
        )
    return 0


def _rows_page_cursors() -> list[dict]:
    with db.get_conn() as conn:
        return conn.execute(
            """
            SELECT source_key, page_name, last_posted_at, last_scan_at, last_found, last_new
            FROM scan_page_cursors ORDER BY last_scan_at DESC
            """
        ).fetchall()



def run_pending(watch_sec: int = 0) -> int:
    """Xử lý hàng đợi scan_requests. watch_sec > 0 => chạy mãi."""
    ctx = _ctx()  # None = không có GraphQL; yêu cầu từ khóa vẫn chạy được bằng browser

    handled = 0
    errors = 0
    while True:
        with db.get_conn() as conn:
            n_requeued = db.requeue_stale_requests(conn)
            if n_requeued:
                print(f"! đưa lại hàng đợi {n_requeued} yêu cầu bị bỏ dở", flush=True)
            req = db.claim_request(conn)

        if not req:
            if not watch_sec:
                print(
                    f"Không còn yêu cầu nào trong hàng đợi."
                    + (f" Đã xử lý {handled} yêu cầu." if handled else "")
                )
                return 0 if errors == 0 else 3
            time.sleep(watch_sec)
            continue

        handled += 1
        status = _run_request(ctx, req)
        if status == "error":
            errors += 1
        with db.get_conn() as conn:
            db.finish_request(
                conn,
                req["id"],
                status=status,
                error=None if status == "done" else "quét lỗi, xem scan_jobs",
            )
        print(f"--- Yêu cầu #{req['id']}: {status}", flush=True)

        if watch_sec:
            time.sleep(watch_sec)


def main(argv=None) -> int:
    args = parse_args(argv)

    if getattr(args, "page_cursors", False):
        return _print_page_cursors()
    if getattr(args, "forget_page", None):
        key = browser_search.page_slug(args.forget_page).lower()
        with db.get_conn() as conn:
            ok = db.delete_page_cursor(conn, key)
        print(("Đã xoá mốc quét: " if ok else "Không có mốc quét: ") + key)
        return 0

    if args.pending:
        return run_pending(args.watch)

    if args.keyword:
        try:
            _cli_range(args)  # báo lỗi sớm nếu --year/--from/--to sai
        except ValueError as exc:
            print(f"LOI: {exc}", file=sys.stderr)
            return 1
        ctx = _ctx()  # None = không có GraphQL; vẫn thử đọc trang bằng browser (--engine auto/browser)
        rc = 0
        for i, term in enumerate(args.keyword):
            rc = max(rc, _run_keyword(ctx, term, args))
            if i < len(args.keyword) - 1:
                delay = random.uniform(config.GROUP_DELAY_MIN, config.GROUP_DELAY_MAX)
                print(f"   nghỉ {delay:.0f}s trước từ khóa kế tiếp...")
                time.sleep(delay)
        return rc

    if args.page:
        try:
            _cli_range(args)
        except ValueError as exc:
            print(f"LOI: {exc}", file=sys.stderr)
            return 1
        rc = 0
        for i, page_ref in enumerate(args.page):
            rc = max(rc, _run_page(None, page_ref, args))
            if i < len(args.page) - 1:
                delay = random.uniform(config.GROUP_DELAY_MIN, config.GROUP_DELAY_MAX)
                print(f"   nghỉ {delay:.0f}s trước Page kế tiếp...")
                time.sleep(delay)
        return rc

    if not (args.all or args.db_group or args.group):
        print(
            "LOI: cần chọn nguồn: --all / --group / --db-group / --keyword / --page\n"
            "     (hoặc --pending, --page-cursors, --forget-page).",
            file=sys.stderr,
        )
        return 1

    if args.year is not None or args.date_from or args.date_to:
        print(
            "LOI: quét feed group chỉ đi từ bài mới về cũ, không nhảy tới năm/ngày cũ được.\n"
            "     Dùng --keyword \"...\" --year YYYY (ô tìm kiếm Facebook) để tìm theo thời gian.",
            file=sys.stderr,
        )
        return 1

    refs: list[str] = []
    if args.all or args.db_group:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, fb_group_id FROM scan_groups WHERE is_active ORDER BY name"
            ).fetchall()
        if args.all:
            refs = [r["fb_group_id"] for r in rows]
        else:
            wanted = set(args.db_group)
            refs = [r["fb_group_id"] for r in rows if r["id"] in wanted]
    refs += args.group

    if not refs:
        print("Không có group nào để quét. Chạy discover.py trước hoặc dùng --group.", file=sys.stderr)
        return 1

    ctx = _ctx()
    if ctx is None:
        return 2
    return _run_groups(ctx, refs, args)


if __name__ == "__main__":
    sys.exit(main())
