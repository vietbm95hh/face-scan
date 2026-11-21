"""Kiểm tra toàn bộ module import OK và cú pháp SQL/hàm chuẩn hoá đúng.

    python scripts/check_install.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fbscan import config, console  # noqa: E402

console.patch_console()


def main() -> int:
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            info = fn()
            print(f"[PASS] {name} -> {info}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name} -> {type(exc).__name__}: {exc}")

    def db_check():
        from fbscan import db

        expected = {
            "scan_groups",
            "scan_posts",
            "scan_post_media",
            "scan_jobs",
            "scan_cursors",
            "scan_requests",
            "scan_keywords",
            "scan_reviews",
            "scan_page_cursors",
        }
        with db.get_conn() as conn:
            row = conn.execute("SELECT current_database() AS d, version() AS v").fetchone()
            tables = {
                r["table_name"]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE 'scan_%'"
                )
            }
            host = conn.execute("SELECT inet_server_addr()::text AS h").fetchone()["h"]
        if host:  # NULL khi server cùng máy
            print(f"       (server đang phục vụ: {host})")
        missing = expected - tables
        if missing:
            raise RuntimeError(
                f"thiếu {len(missing)}/{len(expected)} bảng: {', '.join(sorted(missing))} "
                "— chạy `python -m fbscan.cli init`"
            )
        found = tables & expected
        return f"{row['d']} - {len(found)}/{len(expected)} bảng scan_*"
    def import_check():
        import api.main  # noqa: F401
        import fbscan.discover  # noqa: F401
        import fbscan.scanner  # noqa: F401

        return "api, scanner, discover"

    def playwright_check():
        import playwright  # noqa: F401

        return "đã cài"

    def normalize_check():
        from fbscan.normalize import _to_int, make_title

        assert _to_int("1.2K") == 1200
        assert _to_int(47619) == 47619
        assert _to_int(None) == 0
        assert make_title("\n  Dòng   đầu tiên\nDòng 2") == "Dòng đầu tiên"
        return "ok"

    def cookies_check():
        return "có" if config.COOKIES_FILE.exists() else f"thiếu ({config.COOKIES_FILE})"

    def where_check():
        local = config.PG_HOST.lower() in ("127.0.0.1", "localhost", "::1")
        return "local (máy này)" if local else "server dev (theo .env)"

    def media_check():
        config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.MEDIA_DIR / ".probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return str(config.MEDIA_DIR)

    check("cấu hình DB", lambda: f"{config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}")
    check("nơi đặt DB", where_check)
    check("kết nối PostgreSQL", db_check)
    check("import module", import_check)
    check("playwright", playwright_check)
    check("normalize", normalize_check)
    check("media dir", media_check)
    check("facebook cookies", cookies_check)

    print("\nTất cả ổn." if ok else "\nCó mục cần sửa.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
