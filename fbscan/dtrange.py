"""Khoảng thời gian lọc bài: theo năm hoặc theo ngày.

Facebook trả kết quả tìm kiếm từ rất cũ đến mới, nên ta lọc theo một khoảng
`[date_from, date_to)` (tính trên `posted_at`, giờ UTC).

- Theo năm: `from_year=2025`            → cả năm 2025 (hoặc `from_year..to_year` cho nhiều năm)
- Theo ngày: `from_date="2025-06-01"` → từ đầu ngày đó
            `to_date="2025-06-30"`   → đến hết ngày đó (bao gồm cả ngày 30)
- Hoặc cả hai field trên cùng từng field `from`, `to` (vd `from=2025-06-01`, `to=2025-12-31`)

`to_row_cutoff` trả mốc thời gian của bài CŨ NHẤT được phép lấy, dùng để dừng sớm
khi quét feed/kết quả tìm kiếm (kết quả xếp mới trước nên gặp bài cũ hơn là dừng).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

MIN_YEAR = 2004  # Facebook mở năm 2004
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class DateRange:
    """Khoảng thời gian nửa mở [start, end) theo UTC. Cả hai None = không giới hạn."""

    start: Optional[datetime] = None
    end: Optional[datetime] = None

    @property
    def empty(self) -> bool:
        return self.start is None and self.end is None

    def describe(self) -> str:
        if self.empty:
            return "không giới hạn ngày"

        def day(dt: Optional[datetime]) -> str:
            if not dt:
                return "?"
            if dt.tzinfo is not None:
                dt = dt.astimezone(VN_TZ)
            return dt.strftime("%Y-%m-%d")

        if self.start and self.end:
            end_day = day(self.end - timedelta(days=1))
            if day(self.start) == end_day:
                return day(self.start)
            return f"{day(self.start)} → {end_day}"
        if self.start:
            return f"từ {day(self.start)}"
        return f"đến {day(self.end - timedelta(days=1))}"

    def contains(self, posted_at: Optional[datetime]) -> bool:
        """Bài không có posted_at luôn được nhận (không đủ tin để loại)."""
        if posted_at is None or self.empty:
            return True
        if posted_at.tzinfo is None:  # phòng trường hợp naive -> coi là UTC
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        if self.start and posted_at < self.start:
            return False
        if self.end and posted_at >= self.end:
            return False
        return True

    def row_cutoff(self) -> Optional[datetime]:
        """Mốc bài cũ nhất được lấy (để dừng sớm). None = không giới hạn."""
        return self.start


def _parse_date(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{what} '{value}' không hợp lệ, cần dạng YYYY-MM-DD") from exc


def _parse_year(value, what: str) -> int:
    try:
        year = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} '{value}' không hợp lệ, cần năm dạng 2025") from exc
    if not MIN_YEAR <= year <= 2100:
        raise ValueError(f"{what} phải trong khoảng {MIN_YEAR}..2100 (nhận {year})")
    return year


def _day_start(d: date) -> datetime:
    """00:00 giờ VIỆT NAM của ngày `d`, đổi sang UTC.

    Bài Facebook trả `creation_time` là giờ UTC, nhưng người dùng lọc theo "hôm nay"
    theo giờ Việt Nam. Dùng mốc UTC sẽ bỏ sót bài đăng 00:00–06:59 sáng VN và lọt bài
    sáng hôm sau — vì vậy mọi mốc ngày ở đây đều tính theo giờ VN.
    """
    return datetime(d.year, d.month, d.day, tzinfo=VN_TZ).astimezone(timezone.utc)


def _year_end(year: int) -> datetime:
    return (
        datetime(year, 12, 31, tzinfo=VN_TZ) + timedelta(days=1)
    ).astimezone(timezone.utc)


def build_range(
    *,
    from_year=None,
    to_year=None,
    from_date=None,
    to_date=None,
) -> DateRange:
    """Dựng DateRange từ tham số năm/ngày. Ném ValueError nếu tham số sai.

    - Năm: `from_year` (bắt buộc nếu dùng), `to_year` (bỏ trống = cùng năm)
    - Ngày: `from_date`, `to_date` dạng YYYY-MM-DD (bỏ trống 1 đầu = mở)
    - Không dùng `from_year` cùng `from_date` (mỗi kiểu một cách).
    """
    from_year = None if from_year in (None, "", 0) else from_year
    to_year = None if to_year in (None, "", 0) else to_year
    from_date = (from_date or "").strip() or None
    to_date = (to_date or "").strip() or None

    if from_year is None and to_year is None and not from_date and not to_date:
        return DateRange()

    start: Optional[datetime] = None
    end: Optional[datetime] = None

    if from_year is not None or to_year is not None:
        if not from_year:
            raise ValueError("Dùng năm thì cần 'từ năm' (to_year một mình không xác định được)")
        if from_date or to_date:
            raise ValueError("Chỉ chọn lọc theo NĂM hoặc theo NGÀY, không trộn cả hai")
        y1 = _parse_year(from_year, "từ năm")
        y2 = _parse_year(to_year, "đến năm") if to_year else y1
        if y2 < y1:
            raise ValueError(f"'đến năm' ({y2}) phải >= 'từ năm' ({y1})")
        start = _day_start(date(y1, 1, 1))
        end = _year_end(y2)
        return DateRange(start, end)

    if from_date:
        start = _day_start(_parse_date(from_date, "từ ngày"))
    if to_date:
        d = _parse_date(to_date, "đến ngày")
        end = _day_start(d) + timedelta(days=1)
    if start and end and end <= start:
        raise ValueError("'đến ngày' phải sau 'từ ngày'")
    return DateRange(start, end)


def covered_years(rng: DateRange) -> list[str]:
    """Các năm mà khoảng này phủ trọn (dùng để biết đã quét năm nào)."""
    if rng.empty or not rng.start or not rng.end:
        return []
    years = []
    y = rng.start.year
    last = (rng.end - timedelta(days=1)).year
    while y <= last:
        y_start = _day_start(date(y, 1, 1))
        y_end = _year_end(y)
        if rng.start <= y_start and rng.end >= y_end:
            years.append(str(y))
        y += 1
    return years


def covered_days_count(rng: DateRange) -> Optional[int]:
    if rng.empty or not rng.start or not rng.end:
        return None
    return (rng.end - rng.start).days


__all__ = ["DateRange", "build_range", "covered_years", "covered_days_count"]
