"""Smoke test: ghi 1 group + 2 bài giả rồi kiểm tra toàn bộ API.

    python scripts/smoke_test.py           # chạy test
    python scripts/smoke_test.py --clean   # xoá dữ liệu test
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from fbscan import config, console, db  # noqa: E402

console.patch_console()

TEST_GROUP = "__smoke_test__"


def clean() -> None:
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM scan_requests WHERE keyword_id IN "
            "(SELECT id FROM scan_keywords WHERE term_key IN ('tuyển dụng'))"
        )
        conn.execute("DELETE FROM scan_keywords WHERE term_key='tuyển dụng'")
        conn.execute(
            "DELETE FROM scan_requests WHERE group_id IN "
            "(SELECT id FROM scan_groups WHERE fb_group_id=%s)",
            (TEST_GROUP,),
        )
        conn.execute(
            "DELETE FROM scan_jobs WHERE group_id IN (SELECT id FROM scan_groups WHERE fb_group_id=%s)",
            (TEST_GROUP,),
        )
        conn.execute("DELETE FROM scan_groups WHERE fb_group_id=%s", (TEST_GROUP,))


def seed() -> int:
    with db.get_conn() as conn:
        gid = db.upsert_group(
            conn,
            TEST_GROUP,
            name="Group Test",
            privacy="PUBLIC",
            member_count_txt="1.2K members",
            group_url="https://www.facebook.com/groups/smoke",
        )
        now = datetime.now(timezone.utc)
        posts = [
            {
                "fb_post_id": "test-1",
                "author_fb_id": "u1",
                "author_name": "Nguyễn Văn A",
                "title": "Tuyển dụng lập trình viên Python",
                "content": "Công ty ABC tuyển dụng Python dev, lương 20-30tr.\nLiên hệ inbox.",
                "permalink": "https://www.facebook.com/groups/smoke/posts/test-1",
                "posted_at": now - timedelta(hours=2),
                "like_count": 42,
                "comment_count": 7,
                "share_count": 1,
                "reaction_breakdown": [{"id": "1", "name": "Like", "count": 42}],
                "has_media": True,
                "raw": None,
            },
            {
                "fb_post_id": "test-2",
                "author_fb_id": "u2",
                "author_name": "Trần Thị B",
                "title": "Bán xe máy cũ",
                "content": "Bán Honda Wave 2020, 15 triệu, xe đẹp.",
                "permalink": "https://www.facebook.com/groups/smoke/posts/test-2",
                "posted_at": now - timedelta(days=3),
                "like_count": 5,
                "comment_count": 2,
                "share_count": 0,
                "reaction_breakdown": [],
                "has_media": False,
                "raw": None,
            },
        ]
        ids = []
        for p in posts:
            pid, is_new = db.upsert_post(conn, gid, p)
            assert is_new, f"bài {p['fb_post_id']} phải là mới"
            ids.append(pid)
        db.replace_media(
            conn,
            ids[0],
            [{"kind": "photo", "position": 0, "source_url": "https://example.com/x.jpg", "file_path": None}],
        )
        db.save_cursor(conn, gid, "CURSOR_ABC", True)
        job = db.start_job(conn, gid, "cli")
        db.finish_job(conn, job, status="done", found=2, new=2)

        # Bài của Page (không thuộc group) — quét ô tìm kiếm với search_all
        db.upsert_post(
            conn,
            None,
            {
                "fb_post_id": "test-page-1",
                "author_fb_id": "p1",
                "author_name": "Shop Điện Máy",
                "title": "Khuyến mãi máy lạnh",
                "content": "Giảm giá máy lạnh tháng 11.",
                "permalink": "https://web.facebook.com/shopdienmay/posts/test-page-1",
                "posted_at": now - timedelta(hours=5),
                "like_count": 9,
                "comment_count": 1,
                "share_count": 0,
                "reaction_breakdown": [],
                "has_media": False,
                "raw": None,
                "source_type": "page",
                "source_fb_id": "shopdienmay",
                "source_name": "Shop Điện Máy",
                "source_url": "https://web.facebook.com/shopdienmay/posts/test-page-1",
            },
        )
    return gid


class _FakePost:
    """Bài giả thay cho fgs.Post — đủ thuộc tính mà normalize/_source_of dùng."""

    def __init__(self, pid, ts, url, text, author="Người Đăng"):
        self.id = str(pid)
        self.creation_time = ts
        self.url = url
        self.text = text
        self.title = None
        self.attachments = []
        self.group = None
        self._author = author

    @property
    def owner(self):
        class _O:
            id = "u-" + self.id

            def __init__(self, name):
                self.name = name

        return _O(self._author)

    @property
    def reaction_count(self):
        return 3

    comments_count = 1
    share_count = 0
    top_reaction_breakdown = []


def _test_search_core(gid: int) -> list[tuple[str, bool, object]]:
    """Kiểm tra lõi ô tìm kiếm (lọc năm/ngày, bỏ bài đã có, loại trừ, review) bằng scraper giả."""
    from datetime import datetime, timezone

    from fbscan import dtrange, search_scan

    out: list[tuple[str, bool, object]] = []
    uid = 9900000000001
    ts = lambda y, m, d: int(datetime(y, m, d, 12, tzinfo=timezone.utc).timestamp())

    raw = [
        # 2026: ngoài khoảng năm 2025
        _FakePost(uid + 1, ts(2026, 11, 1), f"https://web.facebook.com/groups/smoke/posts/{uid + 1}",
                  "Tuyển dụng kế toán 2026"),
        # 2025 đúng khoảng
        _FakePost(uid + 2, ts(2025, 6, 15), f"https://web.facebook.com/groups/smoke/posts/{uid + 2}",
                  "Tuyển dụng kế toán tổng hợp, lương 15tr"),
        # 2025 nhưng là bài Page -> bị loại khi search_all=false
        _FakePost(uid + 3, ts(2025, 7, 1), f"https://web.facebook.com/shopabc/posts/{uid + 3}",
                  "Tuyển dụng kế toán tại shop"),
        # 2025 nhưng không chứa từ khóa -> not_match
        _FakePost(uid + 4, ts(2025, 8, 1), f"https://web.facebook.com/groups/smoke/posts/{uid + 4}",
                  "Bán xe máy cũ giá rẻ"),
        # 2025, trùng fb_post_id với bài seed test-1 (đã có trong DB)
        _FakePost("test-1", ts(2025, 9, 1), "https://www.facebook.com/groups/smoke/posts/test-1",
                  "Tuyển dụng kế toán đã có"),
    ]

    class _FakeEngine:
        def __init__(self, _ctx):
            pass

        def search(self, text, filter_type="post"):
            yield from raw

    real_engine = search_scan.fgs.SearchEngine
    real_sleep = search_scan._sleep
    real_sorted = search_scan._sorted_search
    search_scan.fgs.SearchEngine = _FakeEngine
    search_scan._sleep = lambda *_: None

    def _fake_sorted(_ctx, _term, newest_first=True):
        # Đường thật (sort=recent) trả bài mới trước -> fake cũng sắp lại
        yield from sorted(_FakeEngine(_ctx).search(_term), key=lambda p: p.creation_time, reverse=True)

    search_scan._sorted_search = _fake_sorted
    try:
        rng = dtrange.build_range(from_year=2025)

        # preview: không ghi DB
        before = None
        with db.get_conn() as conn:
            before = conn.execute("SELECT count(*)::int c FROM scan_posts").fetchone()["c"]
        pv = search_scan.preview_search(object(), "tuyển dụng kế toán", rng=rng, sample=10)
        with db.get_conn() as conn:
            after = conn.execute("SELECT count(*)::int c FROM scan_posts").fetchone()["c"]
        out.append(("search.preview.no_write", before == after, (before, after)))
        out.append(("search.preview.found", pv["found"] == 5, pv["found"]))
        out.append(("search.preview.in_range", pv["in_range"] == 4, pv["in_range"]))
        out.append(("search.preview.would_save", pv["would_save"] == 1, pv["would_save"]))
        reasons = {i["fb_post_id"]: i["reason"] for i in pv["items"]}
        out.append(("search.preview.reason.out_of_range", reasons.get(str(uid + 1)) == "out_of_range", reasons.get(str(uid + 1))))
        out.append(("search.preview.reason.not_group", reasons.get(str(uid + 3)) == "not_group", reasons.get(str(uid + 3))))
        out.append(("search.preview.reason.not_match", reasons.get(str(uid + 4)) == "not_match", reasons.get(str(uid + 4))))
        out.append(("search.preview.reason.already_in_db", reasons.get("test-1") == "already_in_db", reasons.get("test-1")))

        # loại trừ: bài 2025 sẽ lưu bị exclusion
        pv2 = search_scan.preview_search(object(), "tuyển dụng kế toán", rng=rng, exclusions=[str(uid + 2)])
        out.append(("search.preview.exclusion", pv2["would_save"] == 0 and pv2["skipped"] >= 2, pv2["would_save"]))

        # review: lưu bài đúng + ghi scan_reviews
        rv = search_scan.review_search(object(), "tuyển dụng kế toán", rng=rng, created_by="smoke")
        out.append(("search.review.found", rv["found"] == 5 and rv["in_range"] == 4, (rv["found"], rv["in_range"])))
        out.append(("search.review.new", rv["new_count"] == 1 and rv["new_ids"], rv["new_count"]))
        out.append(("search.review.existing", rv["existing_count"] == 1, rv["existing_count"]))
        with db.get_conn() as conn:
            row = db.get_review(conn, rv["review_id"])
            posted = conn.execute(
                "SELECT posted_at FROM scan_posts WHERE fb_post_id=%s", (str(uid + 2),)
            ).fetchone()
            wrote = conn.execute(
                "SELECT count(*)::int c FROM scan_posts WHERE fb_post_id=%s", (str(uid + 2),)
            ).fetchone()["c"]
        out.append(("search.review.db_row", wrote == 1, wrote))
        out.append(("search.review.date_from", str(row["date_from"]).startswith("2025"), row["date_from"]))
        out.append(
            ("search.review.saved_year_2025",
             posted is not None and posted["posted_at"].year == 2025, posted and posted["posted_at"])
        )

        # lần 2: bài vừa lưu giờ đã có trong DB
        rv2 = search_scan.review_search(object(), "tuyển dụng kế toán", rng=rng, created_by="smoke")
        out.append(("search.review.rerun_no_dup", rv2["new_count"] == 0 and rv2["existing_count"] == 2,
                    (rv2["new_count"], rv2["existing_count"])))

        # lọc theo NGÀY: chỉ 15/06 -> đúng bài uid+2
        day_rng = dtrange.build_range(from_date="2025-06-15", to_date="2025-06-15")
        pv3 = search_scan.preview_search(object(), "tuyển dụng kế toán", rng=day_rng)
        out.append(("search.preview.day_range", pv3["in_range"] == 1, pv3["in_range"]))

        # loại trừ id bài CHƯA lưu (bài đã lưu thì 'already_in_db' đứng trước, cũng bị bỏ)
        pv4 = search_scan.preview_search(object(), "tuyển dụng kế toán", rng=rng,
                                         exclusions=[str(uid + 3)])
        reasons4 = {i["fb_post_id"]: i["reason"] for i in pv4["items"]}
        out.append(("search.preview.excluded_id", reasons4.get(str(uid + 3)) == "excluded_id",
                    reasons4.get(str(uid + 3))))
        pv5 = search_scan.preview_search(object(), "tuyển dụng kế toán", rng=rng,
                                         exclude_sources=["shopabc"])
        out.append(("search.preview.excluded_source",
                    pv5["counts"].get("Nguồn bị loại trừ") == 1, pv5["counts"]))
    finally:
        search_scan.fgs.SearchEngine = real_engine
        search_scan._sleep = real_sleep
        search_scan._sorted_search = real_sorted
        with db.get_conn() as conn:
            conn.execute("DELETE FROM scan_reviews WHERE created_by='smoke'")
            conn.execute("DELETE FROM scan_posts WHERE fb_post_id = %s", (str(uid + 2),))
    return out


def run() -> int:
    clean()
    gid = seed()
    c = TestClient(app)
    checks: list[tuple[str, bool, object]] = []

    r = c.get("/api/health")
    checks.append(("health", r.status_code == 200 and r.json()["ok"], r.json().get("version")))

    r = c.get("/api/groups")
    g = next((x for x in r.json() if x["id"] == gid), None)
    checks.append(("groups.list", bool(g) and g["post_count"] == 2, g and g["post_count"]))

    r = c.get(f"/api/groups/{gid}")
    checks.append(("groups.get", r.status_code == 200, r.json().get("name")))

    r = c.get("/api/posts", params={"group_id": gid})
    body = r.json()
    checks.append(("posts.list", body["total"] == 2 and len(body["items"]) == 2, body["total"]))

    r = c.get("/api/posts", params={"source_type": "page", "q": "máy lạnh"}).json()
    pg = r["items"][0] if r["items"] else {}
    checks.append(
        ("posts.source_page",
         r["total"] == 1 and pg.get("source_name") == "Shop Điện Máy" and pg.get("group_name") is None,
         pg.get("source_name"))
    )
    pgdet = c.get(f"/api/posts/{pg.get('id')}").json() if pg else {}
    checks.append(("posts.source_page_detail", pgdet.get("source_type") == "page", pg.get("id")))
    first_with_media = next((x for x in body["items"] if x["fb_post_id"] == "test-1"), None)
    checks.append(
        ("posts.cover_url",
         first_with_media is not None and first_with_media.get("cover_url") == "https://example.com/x.jpg",
         first_with_media and first_with_media.get("cover_url"))
    )

    r = c.get("/api/posts", params={"group_id": gid, "q": "python"})
    checks.append(("posts.search", r.json()["total"] == 1, r.json()["total"]))
    r = c.get("/api/posts", params={"group_id": gid, "has_media": True})
    checks.append(("posts.has_media", r.json()["total"] == 1, r.json()["total"]))

    r = c.get("/api/posts", params={"group_id": gid, "sort": "like_count", "order": "desc"})
    first = r.json()["items"][0]
    checks.append(("posts.sort", first["like_count"] == 42, first["like_count"]))

    pid = body["items"][0]["id"]
    r = c.get(f"/api/posts/{pid}")
    checks.append(("posts.get", r.status_code == 200 and r.json()["media"] is not None, r.status_code))

    r = c.get("/api/posts/999999999")
    checks.append(("posts.404", r.status_code == 404, r.status_code))

    r = c.get("/api/stats", params={"group_id": gid, "days": 7})
    st = r.json()
    checks.append(("stats", st["total"] == 2 and len(st["by_day"]) >= 2, st["total"]))

    r = c.get("/api/jobs", params={"status": "done"})
    checks.append(("jobs", r.status_code == 200 and len(r.json()) >= 1, len(r.json())))

    # --- hàng đợi quét theo group + khoảng thời gian
    r = c.post("/api/scan", json={"group_id": gid, "days": 3, "max_posts": 10})
    req = r.json()
    checks.append(
        ("scan.create", r.status_code == 201 and req["status"] == "queued" and req["days"] == 3,
         req.get("id"))
    )
    checks.append(
        ("scan.bad_group", c.post("/api/scan", json={"group": "khong_ton_tai_xyz"}).status_code == 404, True)
    )
    checks.append(
        ("scan.empty", c.post("/api/scan", json={}).status_code == 422, True)
    )
    checks.append(
        ("scan.get", c.get(f"/api/scan/{req['id']}").json()["status"] == "queued", req["id"])
    )
    st = c.get("/api/scan/status").json()
    checks.append(("scan.status", st["queued"] >= 1, st["queued"]))
    checks.append(
        ("scan.cancel", c.delete(f"/api/scan/{req['id']}").status_code == 200, req["id"])
    )
    checks.append(
        ("scan.cancel.404", c.delete("/api/scan/999999999").status_code == 404, True)
    )

    # --- hàng đợi quét theo từ khóa (ô tìm kiếm Facebook)
    r = c.post("/api/scan", json={"keyword": "  Tuyển   Dụng ", "days": 3, "max_posts": 10})
    kreq = r.json()
    checks.append(
        ("scan.create_keyword",
         r.status_code == 201 and kreq["status"] == "queued" and kreq["keyword_id"]
         and kreq["group_id"] is None and kreq["keyword"] == "Tuyển Dụng",
         kreq.get("keyword"))
    )
    checks.append(("scan.keyword_search_all_default", kreq["search_all"] is False, kreq["search_all"]))
    r3 = c.post("/api/scan", json={"keyword": "tuyển dụng", "search_all": True}).json()
    checks.append(("scan.keyword_search_all", r3["search_all"] is True, r3["search_all"]))
    c.delete(f"/api/scan/{r3['id']}")
    r2 = c.post("/api/scan", json={"keyword": "tuyển dụng", "max_posts": 5}).json()
    checks.append(("scan.keyword_reuse", r2["keyword_id"] == kreq["keyword_id"], r2["keyword_id"]))
    checks.append(
        ("scan.keyword_filter",
         any(x["id"] == kreq["id"] for x in c.get(
             "/api/scan", params={"keyword_id": kreq["keyword_id"]}).json()),
         kreq["keyword_id"])
    )
    kws = c.get("/api/keywords").json()
    checks.append(("keywords.list", any(k["id"] == kreq["keyword_id"] for k in kws), len(kws)))
    checks.append(
        ("scan.mixed_both_422",
         c.post("/api/scan", json={"keyword": "abc", "group_id": gid}).status_code == 422, True)
    )
    checks.append(
        ("scan.keyword_only_spaces_422",
         c.post("/api/scan", json={"keyword": "   "}).status_code == 422, True)
    )
    c.delete(f"/api/scan/{kreq['id']}")
    c.delete(f"/api/scan/{r2['id']}")
    checks.append(
        ("keywords.delete", c.delete(f"/api/keywords/{kreq['keyword_id']}").status_code == 200, True)
    )
    checks.append(
        ("keywords.delete.404", c.delete("/api/keywords/999999999").status_code == 404, True)
    )

    # --- lọc theo NĂM / NGÀY khi đặt yêu cầu
    r = c.post("/api/scan", json={"keyword": "tuyển dụng", "from_year": 2025})
    yr = r.json()
    checks.append(
        ("scan.keyword_year",
         r.status_code == 201 and yr["date_from"].startswith("2025-01-01")
         and yr["date_to"].startswith("2026-01-01") and yr["days"] == 0,
         (yr.get("date_from"), yr.get("date_to")))
    )
    c.delete(f"/api/scan/{yr['id']}")
    r = c.post("/api/scan", json={"keyword": "tuyển dụng", "from_date": "2025-06-01", "to_date": "2025-06-30"})
    adv = r.json()
    checks.append(
        ("scan.keyword_days_range",
         r.status_code == 201 and adv["date_from"].startswith("2025-06-01")
         and adv["date_to"].startswith("2025-07-01"),
         (adv.get("date_from"), adv.get("date_to")))
    )
    c.delete(f"/api/scan/{adv['id']}")
    checks.append(
        ("scan.days_and_range_422",
         c.post("/api/scan", json={"keyword": "abc", "days": 7, "from_year": 2025}).status_code == 422, True)
    )
    checks.append(
        ("scan.bad_year_422",
         c.post("/api/scan", json={"keyword": "abc", "from_year": 1999}).status_code == 422, True)
    )
    r = c.post("/api/scan", json={"keyword": "tuyển dụng", "exclusions": ["test-1"], "excluded_terms": ["spam"]})
    ex = r.json()
    checks.append(("scan.exclusions_saved", ex["exclusions"] == ["test-1"] and ex["excluded_terms"] == ["spam"], ex["exclusions"]))
    c.delete(f"/api/scan/{ex['id']}")

    # --- lõi tìm kiếm/review với scraper giả (không gọi Facebook thật)
    checks.extend(_test_search_core(gid))

    r = c.get("/media/../../.env")
    checks.append(("media.traversal", r.status_code == 404, r.status_code))

    ok = True
    for name, passed, info in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name} -> {info}")
        ok = ok and passed

    clean()
    print("\nTất cả pass." if ok else "\nCó test FAIL.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    if args.clean:
        clean()
        print("Đã xoá dữ liệu test.")
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
