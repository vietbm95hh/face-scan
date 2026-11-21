"""Unit test nhanh cho lọc thời gian + so khớp từ khóa (không cần Facebook).

    python scripts/test_filters.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbscan import console, dtrange  # noqa: E402
from fbscan.search_scan import score_match, strip_accents  # noqa: E402

console.patch_console()

UTC = timezone.utc
checks: list[tuple[str, bool, object]] = []


def check(name, cond, info=None):
    checks.append((name, bool(cond), info))


# ---------------------------------------------------------------- năm / ngày
r = dtrange.build_range(from_year=2025)
check("year.start", r.start == datetime(2025, 1, 1, tzinfo=UTC), r.start)
check("year.end", r.end == datetime(2026, 1, 1, tzinfo=UTC), r.end)
check("year.contains.in", r.contains(datetime(2025, 6, 1, tzinfo=UTC)), True)
check("year.contains.edge1", r.contains(datetime(2025, 1, 1, tzinfo=UTC)), True)
check("year.contains.edge2", r.contains(datetime(2025, 12, 31, 23, 59, tzinfo=UTC)), True)
check("year.contains.out_prev", not r.contains(datetime(2024, 12, 31, 23, 59, tzinfo=UTC)), True)
check("year.contains.out_next", not r.contains(datetime(2026, 1, 1, tzinfo=UTC)), True)
check("year.describe", r.describe() == "2025-01-01 → 2025-12-31", r.describe())
check("year.row_cutoff", r.row_cutoff() == datetime(2025, 1, 1, tzinfo=UTC), r.row_cutoff())

r2 = dtrange.build_range(from_year=2024, to_year=2025)
check("year.range", r2.start.year == 2024 and r2.end == datetime(2026, 1, 1, tzinfo=UTC), (r2.start, r2.end))

r3 = dtrange.build_range(from_date="2025-06-01", to_date="2025-06-30")
check("day.end_inclusive_of_to_date", r3.end == datetime(2025, 7, 1, tzinfo=UTC), r3.end)
check("day.contains.last", r3.contains(datetime(2025, 6, 30, 23, 0, tzinfo=UTC)), True)
check("day.describe", r3.describe() == "2025-06-01 → 2025-06-30", r3.describe())

r4 = dtrange.build_range(from_date="2025-06-01")
check("day.open_end", r4.start == datetime(2025, 6, 1, tzinfo=UTC) and r4.end is None, (r4.start, r4.end))

check("range.empty", dtrange.build_range().empty, True)
check("range.none_contains", dtrange.DateRange().contains(datetime(2001, 1, 1, tzinfo=UTC)), True)
check("range.null_posted", r3.contains(None), True)
check("covered_years", dtrange.covered_years(r) == ["2025"], dtrange.covered_years(r))
check("covered_years.partial", dtrange.covered_years(r3) == [], dtrange.covered_years(r3))

for bad, label in [
    (dict(from_year=1999), "year too old"),
    (dict(from_year=2021, to_year=2020), "to < from"),
    (dict(from_date="2021-13-01"), "bad date"),
    (dict(from_date="2025-06-30", to_date="2025-06-01"), "date order"),
    (dict(from_year=2025, from_date="2025-01-01"), "mixed"),
]:
    try:
        dtrange.build_range(**bad)
        check(f"invalid.{label}", False, "không ném lỗi")
    except ValueError as exc:
        check(f"invalid.{label}", True, str(exc)[:60])

# ---------------------------------------------------------------- so khớp
check("accent", strip_accents("Tuyển Dụng Kế Toán") == "tuyen dung ke toan", strip_accents("Tuyển Dụng Kế Toán"))
check("accent.d", strip_accents("Đắk Lắk") == "dak lak", strip_accents("Đắk Lắk"))

lvl, missing = score_match("Cần tuyển dụng kế toán tổng hợp", "tuyển dụng kế toán")
check("match.exact", lvl == "exact", lvl)
# --- chỉ 1 ngày: from_date = to_date
r5 = dtrange.build_range(from_date="2025-11-09", to_date="2025-11-09")
check("same_day.end", r5.end == datetime(2025, 11, 10, tzinfo=UTC), r5.end)
check("same_day.in_start", r5.contains(datetime(2025, 11, 9, 0, 0, tzinfo=UTC)), True)
check("same_day.in_end", r5.contains(datetime(2025, 11, 9, 23, 59, 59, tzinfo=UTC)), True)
check("same_day.out", not r5.contains(datetime(2025, 11, 10, 0, 0, 1, tzinfo=UTC)), True)
check("same_day.describe", r5.describe() == "2025-11-09", r5.describe())

lvl, _ = score_match("Tuyen dung KẾ TOÁN gấp", "tuyển dụng kế toán")
check("match.accent_insensitive", lvl == "exact", lvl)
lvl, missing = score_match("Kế toán tuyển gấp, dụng cụ đủ", "tuyển dụng kế toán")
check("match.slug_all_tokens", lvl == "loose" and missing == [], (lvl, missing))  # thứ tự khác từ khóa
lvl, missing = score_match("Kế toán cần người gấp", "tuyển dụng kế toán")
check("match.loose_core_phrase", lvl == "loose" and set(missing) == {"tuyen", "dung"}, (lvl, missing))
check("match.missing_tokens", set(missing) == {"tuyen", "dung"}, missing)
lvl, missing = score_match("Kế toán tuyển gấp, cần người", "tuyển dụng kế toán")
check("match.loose_missing_aux", lvl == "loose" and missing == ["dung"], (lvl, missing))
lvl, missing = score_match("Tuyển nhân viên bán hàng", "tuyển dụng kế toán")
check("match.weak", lvl == "weak" and "dung" in missing, (lvl, missing))
lvl, _ = score_match("Bán xe máy cũ", "tuyển dụng kế toán")
check("match.none", lvl == "none", lvl)
lvl, _ = score_match(None, "abc")
check("match.none.text", lvl == "none", lvl)

# --- "có từ khóa trong title/nội dung" theo chế độ strict
check("strict.partial_phrase_aux", score_match("Tuyển dụng nhân viên kinh doanh", "tuyển dụng kế toán")[0] == "loose", True)
check("strict.long_heading", score_match("TUYỂN DỤNG KẾ TOÁN - BÁN HÀNG", "tuyển dụng kế toán")[0] == "exact", True)
check("strict.partial_phrase", score_match("Tuyển dụng nhân viên kinh doanh", "tuyển dụng kế toán")[0] == "loose", True)
lvl, miss = score_match("Tuyển nhân viên bán hàng", "tuyển dụng kế toán")
check("strict.no_partial_phrase", lvl == "weak" and "ke" in miss and "toan" in miss, (lvl, miss))
check("strict.aux_word_only", score_match("Dụng cụ văn phòng giá rẻ", "tuyển dụng kế toán")[0] == "weak", True)
check(
    "strict.rejects_partial",
    score_match("Tuyển nhân viên bán hàng", "tuyển dụng kế toán")[0] not in ("exact", "slug"),
    True,
)


from fbscan import search_scan  # noqa: E402


def _Hit(text):
    return search_scan.SearchHit(
        fb_post_id="x", posted_at=None, url=None, text=text, author_name=None,
        source_type="group", source_fb_id="1", source_name="g", in_range=True,
    )

for mode, text, want in [
    ("strict", "Tuyển dụng kế toán gấp", True),
    ("strict", "Kế toán tuyển gấp, dụng cụ đủ", True),
    ("strict", "TUYỂN DỤNG KẾ TOÁN - BÁN HÀNG", True),
    ("strict", "Kế toán cần người gấp", True),  # "ke toan" là cụm liền nhau của từ khóa
    ("strict", "Tuyển nhân viên bán hàng", False),
    ("strict", "Dụng cụ văn phòng giá rẻ", False),
    ("strict", "Bán xe máy cũ", False),
    ("normal", "Kế toán cần người gấp", True),
    ("loose", "Tuyển nhân viên bán hàng", True),
    ("loose", "Kế toán cần người gấp", True),
    ("loose", "Bán xe máy cũ", False),
    ("off", "Bán xe máy cũ", True),
]:
    hit = _Hit(text)
    plan = search_scan.build_plan("tuyển dụng kế toán", keyword_match=mode)
    check(f"mode.{mode}:{text[:24]}", search_scan.matches(hit, plan) is want, hit.level)

ok = True
for name, passed, info in checks:
    print(f"[{'PASS' if passed else 'FAIL'}] {name} -> {info}")
    ok = ok and passed
print(f"\n{sum(1 for c in checks if c[1])}/{len(checks)} pass")
sys.exit(0 if ok else 1)
