"""Đảm bảo stdout/stderr in được tiếng Việt trên console Windows (cp1252)."""

from __future__ import annotations

import sys


def patch_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
