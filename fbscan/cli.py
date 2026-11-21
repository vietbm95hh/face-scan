"""CLI quản trị nhỏ: init DB, liệt kê group, bật/tắt, reset cursor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fbscan import config, db, console  # noqa: E402

console.patch_console()


def cmd_init(_args) -> int:
    files = sorted((config.ROOT / "migrations").glob("*.sql"))
    if not files:
        print("Không thấy file migration nào trong migrations/", file=sys.stderr)
        return 1
    with db.get_conn() as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"  OK: {f.name}")
    print(f"OK: Đã khởi tạo schema trên {config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}")
    return 0


def cmd_groups(_args) -> int:
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT g.id, g.fb_group_id, g.name, g.is_active, g.last_scanned_at,
                   (SELECT count(*) FROM scan_posts p WHERE p.group_id=g.id) AS posts,
                   c.cursor IS NOT NULL AS has_cursor
            FROM scan_groups g
            LEFT JOIN scan_cursors c ON c.group_id=g.id
            ORDER BY g.name
        """).fetchall()
    if not rows:
        print("(chưa có group nào)")
        return 0
    print(f"{'id':>5}  {'posts':>6}  {'on':>2}  {'cursor':>6}  {'last_scan':<20}  name")
    for r in rows:
        last = r["last_scanned_at"].strftime("%Y-%m-%d %H:%M") if r["last_scanned_at"] else "-"
        print(
            f"{r['id']:>5}  {r['posts']:>6}  {'Y' if r['is_active'] else 'N':>2}  "
            f"{'Y' if r['has_cursor'] else '-':>6}  {last:<20}  {r['name'] or r['fb_group_id']}"
        )
    return 0


def cmd_toggle(args) -> int:
    with db.get_conn() as conn:
        row = conn.execute(
            "UPDATE scan_groups SET is_active=%s WHERE id=%s RETURNING name, is_active",
            (args.on, args.id),
        ).fetchone()
    if not row:
        print("Không thấy group id", args.id)
        return 1
    print(f"OK: {row['name']}: is_active={row['is_active']}")
    return 0


def cmd_reset(args) -> int:
    with db.get_conn() as conn:
        if args.id:
            db.reset_cursor(conn, args.id)
            print(f"OK: reset cursor group {args.id}")
        else:
            conn.execute("DELETE FROM scan_cursors")
            print("OK: reset cursor tất cả group")
    return 0


def cmd_jobs(_args) -> int:
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT j.*, g.name FROM scan_jobs j
            LEFT JOIN scan_groups g ON g.id=j.group_id
            ORDER BY j.id DESC LIMIT 30
        """).fetchall()
    for r in rows:
        print(
            f"#{r['id']:<5} {r['status']:<8} {r['name'] or '-':<30} "
            f"found={r['posts_found']:<4} new={r['posts_new']:<4} err={r['error'] or ''}"
        )
    return 0


def cmd_keywords(_args) -> int:
    with db.get_conn() as conn:
        rows = db.list_keywords(conn)
    if not rows:
        print("(chưa có từ khóa nào)")
        return 0
    print(f"{'id':>4}  {'posts':>6}  {'last_scan':<20}  từ khóa")
    for r in rows:
        last = r["last_scraped_at"].strftime("%Y-%m-%d %H:%M") if r["last_scraped_at"] else "-"
        print(f"{r['id']:>4}  {r['post_count']:>6}  {last:<20}  {r['term']}")
    return 0


def cmd_requests(args) -> int:
    with db.get_conn() as conn:
        rows = db.list_requests(conn, status=args.status, limit=args.limit)
    if not rows:
        print("(hàng đợi trống)")
        return 0
    print(f"{'id':>4}  {'status':<9}  {'group / từ khóa':<32}  {'days':>4}  {'max':>5}  created")
    for r in rows:
        if r.get("keyword_id"):
            target = f"kw: {r.get('keyword') or r['keyword_id']}"
        else:
            target = r["group_name"] or r["fb_group_id"] or str(r["group_id"])
        created = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "-"
        print(
            f"{r['id']:>4}  {r['status']:<9}  {target[:32]:<32}  "
            f"{r['days'] or 'all':>4}  {r['max_posts'] or 'all':>5}  {created}"
            + (f"  err={r['error'][:40]}" if r["error"] else "")
        )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fbscan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="tạo bảng từ migrations/001_init.sql").set_defaults(fn=cmd_init)
    sub.add_parser("groups", help="liệt kê group").set_defaults(fn=cmd_groups)
    sub.add_parser("jobs", help="30 lần quét gần nhất").set_defaults(fn=cmd_jobs)
    sub.add_parser("keywords", help="từ khóa đã quét bằng ô tìm kiếm FB").set_defaults(
        fn=cmd_keywords
    )

    q = sub.add_parser("requests", help="hàng đợi yêu cầu quét (từ API)")
    q.add_argument(
        "--status",
        choices=["queued", "running", "done", "error", "canceled"],
        default=None,
    )
    q.add_argument("--limit", type=int, default=30)
    q.set_defaults(fn=cmd_requests)

    t = sub.add_parser("toggle", help="bật/tắt group")
    t.add_argument("id", type=int)
    t.add_argument("--on", type=lambda s: s.lower() not in ("0", "false", "off"))
    t.set_defaults(fn=cmd_toggle)

    r = sub.add_parser("reset", help="xoá cursor")
    r.add_argument("--id", type=int)
    r.set_defaults(fn=cmd_reset)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
