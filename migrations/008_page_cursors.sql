-- =====================================================================
-- 008: mốc quét feed Page — lần sau chỉ quét tiếp bài mới
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Mỗi Page 1 dòng: thời điểm đăng của bài MỚI NHẤT đã quét xong.
-- Lần quét sau dùng mốc này để dừng sớm khi cuộn qua bài cũ hơn, thay vì
-- cuộn lại toàn bộ feed. Mốc chỉ tiến, không lùi (xem db.save_page_cursor).
CREATE TABLE IF NOT EXISTS scan_page_cursors (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_key      text        NOT NULL UNIQUE,   -- slug/id Page, lowercase
    page_url        text,
    page_name       text,
    last_posted_at  timestamptz,                   -- posted_at của bài mới nhất đã quét
    last_fb_post_id text,
    last_scan_at    timestamptz NOT NULL DEFAULT now(),
    last_found      integer     NOT NULL DEFAULT 0,
    last_new        integer     NOT NULL DEFAULT 0,
    note            text
);

CREATE INDEX IF NOT EXISTS ix_page_cursors_scanned ON scan_page_cursors (last_scan_at DESC);
