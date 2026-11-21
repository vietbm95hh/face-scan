"""Test nhanh endpoint đối chiếu qua HTTP (TestClient), scraper giả — không gọi Facebook.

    python scripts/test_search_api.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from fbscan import console, context as fbctx, db, search_scan  # noqa: E402

console.patch_console()

checks: list[tuple[str, bool, object]] = []


def check(name, cond, info=None):
    checks.append((name, bool(cond), info))


class FakePost:
    def __init__(self, pid, ts, url, text):
        self.id = str(pid)
        self.creation_time = ts
        self.url = url
        self.text = text
        self.title = None
        self.attachments = []
        self.group = None
        self.reaction_count = 1
        self.comments_count = 0
        self.share_count = 0
        self.top_reaction_breakdown = []

    @property
    def owner(self):
        class O:
            id = "u1"
            name = "Người Thử"

        return O()


def _today():
    from datetime import datetime as _dt

    from zoneinfo import ZoneInfo

    return _dt.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()


def _today_of_range(range_from, range_to):
    """Khoảng của 'hôm nay' phải đúng 1 ngày: từ hôm nay (giờ VN) đến hôm sau."""
    return range_from[:10]


class FakeEngine:
    def __init__(self, _ctx):
        pass

    def search(self, text, filter_type="post"):
        t = lambda y, m, d: int(datetime(y, m, d, 8, tzinfo=timezone.utc).timestamp())
        y, m, d = (int(x) for x in _today().split("-"))
        # bài hôm nay: tiêu đề KHÔNG chứa từ khóa đủ ý, chỉ trùng "kế"
        yield FakePost("9900000000199", t(y, m, d), "https://web.facebook.com/groups/smoke/posts/9900000000199",
                       "Kế hoạch bán hàng tháng 11")
        # bài hôm nay, CÓ từ khóa đủ ý -> phải lọc theo ngày vẫn giữ
        yield FakePost("9900000000198", t(y, m, d), "https://web.facebook.com/groups/smoke/posts/9900000000198",
                       "Tuyển dụng kế toán hôm nay, lương thỏa thuận")
        yield FakePost("9900000000101", t(2024, 3, 1), "https://web.facebook.com/groups/smoke/posts/9900000000101",
                       "Tuyển dụng kế toán năm 2024")
        yield FakePost("9900000000102", t(2025, 5, 5), "https://web.facebook.com/groups/smoke/posts/9900000000102",
                       "Tuyển dụng kế toán năm 2025")
        yield FakePost("9900000000103", t(2025, 6, 6), "https://web.facebook.com/groups/smoke/posts/9900000000103",
                       "Tuyển dụng kế toán tiếp nữa")


def main() -> int:
    key = "kế toán HTTP test"
    kw_id = None
    with db.get_conn() as conn:
        kw = db.upsert_keyword(conn, key)
        kw_id = kw["id"]

    real_engine, real_sleep, real_ctx = search_scan.fgs.SearchEngine, search_scan._sleep, fbctx.make_context
    real_sorted = search_scan._sorted_search
    search_scan.fgs.SearchEngine = FakeEngine
    # fake không hỗ trợ sort GraphQL -> dùng đường thường (giữ nguyên dữ liệu giả)
    def fake_sorted(_ctx, _term, newest_first=True):
        # Đường thật luôn trả bài MỚI trước (sort=recent) -> fake cũng sắp lại
        posts = sorted(FakeEngine(_ctx).search(_term), key=lambda p: p.creation_time, reverse=True)
        yield from posts

    search_scan._sorted_search = fake_sorted
    search_scan._sleep = lambda *_: None
    fbctx.make_context = lambda **_: object()
    import api.main as m

    m._need_ctx = lambda: object()
    try:
        c = TestClient(app)

        r = c.post("/api/search/preview", json={"keyword": key, "from_year": 2025})
        body = r.json()
        check("preview.status", r.status_code == 200, r.status_code)
        check("preview.found", body["found"] == 5, body["found"])
        check("preview.in_range", body["in_range"] == 2, body["in_range"])
        check("preview.would_save", body["would_save"] == 2, body["would_save"])
        check("preview.range", body["range"] == "2025-01-01 → 2025-12-31", body.get("range"))

        r = c.post("/api/search/preview", json={"keyword": key, "from_year": 2025, "days": 3})
        check("preview.days_and_year_422", r.status_code == 422, r.status_code)

        # review chạy nền trong task này (TestClient chạy background task ngay khi response xong)
        r = c.post("/api/search/review", json={"keyword": key, "from_year": 2025, "search_all": False})
        check("review.start", r.status_code == 200 and r.json()["status"] == "started", r.json().get("range"))

        rv = c.get("/api/search/reviews", params={"keyword_id": kw_id}).json()
        check("review.list", len(rv) >= 1, len(rv))
        rid = rv[0]["id"]
        check("review.list.new_count", rv[0]["new_count"] == 2, rv[0]["new_count"])

        d = c.get(f"/api/search/reviews/{rid}").json()
        check("review.detail.new_posts", len(d.get("new_posts", [])) == 2, len(d.get("new_posts", [])))
        check("review.detail.reasons", "out_of_range" in d.get("reasons", {}), list(d.get("reasons", {}))[:3])
        check("review.detail.range", d["range"] == "2025-01-01 → 2025-12-31", d.get("range"))

        # review lại: đã có trong DB
        c.post("/api/search/review", json={"keyword": key, "from_year": 2025, "search_all": False})
        d2 = c.get(f"/api/search/reviews/{rid + 1}").json()
        check("review.rerun.existing", d2["new_count"] == 0 and d2["existing_count"] == 2,
              (d2["new_count"], d2["existing_count"]))

        check("review.404", c.get("/api/search/reviews/999999999").status_code == 404, True)

        # --- "chỉ bài trong ngày": date = 1 ngày -> date_from..date_to đúng 1 ngày
        r = c.post("/api/scan", json={"keyword": key, "date": "2025-11-09"}).json()
        check("scan.date_today",
              r["date_from"].startswith("2025-11-09") and r["date_to"].startswith("2025-11-10"),
              (r["date_from"], r["date_to"]))
        check("scan.date_keyword_match_default", r["keyword_match"] == "strict", r["keyword_match"])
        c.delete(f"/api/scan/{r['id']}")
        check("scan.date_plus_from_422",
              c.post("/api/scan", json={"keyword": key, "date": "2025-11-09", "from_year": 2025}).status_code == 422,
              True)

        # --- keyword_match: strict loại bài chỉ trùng 1 từ; loose nhận
        from fbscan import dtrange

        day = dtrange.build_range(from_date=_today(), to_date=_today())
        pv_s = search_scan.preview_search(object(), key, rng=day, keyword_match="strict")
        pv_l = search_scan.preview_search(object(), key, rng=day, keyword_match="loose")
        # hôm nay có 2 bài: 199 (chỉ trùng "kế") + 198 (đủ từ khóa)
        check("match.strict.skips_weak",
              pv_s["would_save"] == 1 and pv_s["counts"].get("Nội dung không chứa từ khóa", 0) == 1,
              pv_s["counts"])
        check("match.loose.saves_weak", pv_l["would_save"] == 2, pv_l["counts"])
        check("match.match_counts", "Chỉ trùng một từ rời" in pv_s["match_counts"], pv_s["match_counts"])

        # --- BUG: UI gửi date="" (chọn "Chỉ hôm nay") -> phải lọc đúng hôm nay, không phải "không lọc"
        r = c.post("/api/search/preview", json={"keyword": key, "date": "", "search_all": True}).json()
        check("preview.date_empty_is_today",
              r.get("range_from") is not None
              and r["range_from"].startswith(_today_of_range(r["range_from"], r["range_to"])),
              (r.get("range_from"), r.get("range_to")))
        check("preview.date_empty_keeps_today",
              r["in_range"] >= 1 and r["would_save"] == 1,
              (r["found"], r["in_range"], r["would_save"]))
        # Ngày: chỉ bài hôm nay được coi là in_range; bài 2024/2025 phải ra ngoài khoảng.
        inrange = {i["fb_post_id"] for i in r["items"] if i["in_range"]}
        check("preview.date_empty_only_today",
              inrange == {"9900000000198", "9900000000199"},
              [(i["fb_post_id"], i["in_range"]) for i in r["items"]][:5])
    finally:
        search_scan.fgs.SearchEngine, search_scan._sleep, fbctx.make_context = real_engine, real_sleep, real_ctx
        search_scan._sorted_search = real_sorted
        with db.get_conn() as conn:
            conn.execute("DELETE FROM scan_reviews WHERE keyword_id=%s", (kw_id,))
            for pid in ("9900000000101", "9900000000102", "9900000000103", "9900000000199", "9900000000198"):
                conn.execute("DELETE FROM scan_posts WHERE fb_post_id=%s", (pid,))
            conn.execute("DELETE FROM scan_keywords WHERE id=%s", (kw_id,))

    ok = True
    for name, passed, info in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} -> {info}")
        ok = ok and passed
    print(f"\n{sum(1 for c in checks if c[1])}/{len(checks)} pass")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
