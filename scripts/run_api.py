"""Chạy API: python scripts/run_api.py [--reload]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from fbscan import config, db  # noqa: E402


def _preflight() -> None:
    """In DB đang dùng và kiểm tra kết nối trước khi mở API."""
    target = f"{config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}"
    print(f"DB: {target}", flush=True)
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - chỉ cảnh báo, API vẫn chạy
        print(
            f"DB: KHÔNG kết nối được — {type(exc).__name__}: {exc}\n"
            "  Kiểm tra Tailscale/mạng tới server rồi chạy lại. "
            "API vẫn khởi động nhưng sẽ trả 503 khi gọi DB.",
            file=sys.stderr,
            flush=True,
        )
    else:
        print("DB: OK", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--host", default=config.API_HOST)
    ap.add_argument("--port", type=int, default=config.API_PORT)
    args = ap.parse_args()

    _preflight()
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "api"), str(ROOT / "fbscan")] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
