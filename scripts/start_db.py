"""Bật PostgreSQL cluster local — chỉ dùng khi DB chạy trên máy này.

Chạy được từ PowerShell, cmd hay bash — KHÔNG mở cửa sổ phụ, KHÔNG chờ Enter:

    python scripts/start_db.py

Nếu `.env` trỏ tới DB trên server dev (PG_HOST khác 127.0.0.1/localhost) thì
script báo rõ địa chỉ, kiểm tra kết nối tới đó và thoát ngay — không bật
PostgreSQL local.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbscan import config, console  # noqa: E402

console.patch_console()

PG_BIN = Path(r"D:\laragon\bin\postgresql\postgresql\bin")
PG_DATA = Path(r"D:\laragon\data\postgresql2")
PG_PORT = 2024
HOST = "127.0.0.1"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def is_ready() -> bool:
    try:
        r = subprocess.run(
            [str(PG_BIN / "pg_isready.exe"), "-h", HOST, "-p", str(PG_PORT), "-q"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def port_in_use() -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((HOST, PG_PORT)) == 0


def remote_ready() -> bool:
    """Thử kết nối tới PostgreSQL trên server dev (có timeout ngắn)."""
    try:
        with socket.create_connection((config.PG_HOST, config.PG_PORT), timeout=5):
            return True
    except OSError:
        return False


def main() -> int:
    if config.PG_HOST.lower() not in ("127.0.0.1", "localhost", "::1"):
        target = f"{config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}"
        if remote_ready():
            print(f".env dùng DB trên server dev: {target} — kết nối được, không bật PostgreSQL local.")
            return 0
        print(
            f".env dùng DB trên server dev: {target} — KHÔNG kết nối được.\n"
            "  - Kiểm tra server dev có bật PostgreSQL không, mạng/VPN/Tailscale đã thông chưa.\n"
            "  - Kiểm tra lại PG_HOST/PG_PORT trong .env.",
            file=sys.stderr,
        )
        return 1

    if is_ready():
        print(f"PostgreSQL {PG_PORT} đã chạy sẵn.")
        return 0

    if not (PG_DATA / "PG_VERSION").exists():
        print(f"Không thấy data dir: {PG_DATA}", file=sys.stderr)
        return 2

    if port_in_use():
        print(
            f"Cổng {PG_PORT} đang bị process khác chiếm — không khởi động được.",
            file=sys.stderr,
        )
        return 3

    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS: không bị kill khi shell cha thoát, không mở cửa sổ.
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    log_file = PG_DATA / "log" / "stdout.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log:
        subprocess.Popen(
            [str(PG_BIN / "postgres.exe"), "-D", str(PG_DATA)],
            cwd=str(PG_BIN),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=creationflags,
            close_fds=True,
        )

    for _ in range(60):
        if is_ready():
            print(f"PostgreSQL {PG_PORT} đã sẵn sàng.")
            return 0
        time.sleep(1)

    print(f"Khởi động thất bại — xem log {PG_DATA / 'log'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
