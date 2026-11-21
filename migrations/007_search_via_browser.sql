-- =====================================================================
-- 007: tìm bài qua browser (Playwright) thay cho GraphQL khi FB chặn
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Bài lấy bằng cách đọc trang tìm kiếm (không có dữ liệu GraphQL đầy đủ).
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS fetched_by text;

CREATE INDEX IF NOT EXISTS ix_posts_fetched_by ON scan_posts (fetched_by);

-- Nguồn của mỗi bài: group (link /groups/<id>|slug/posts/<id>) hoặc profile
-- (link /<username>/posts/<id>) — dùng để dedupe/upsert giữa các lần quét.
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS source_path text;

CREATE INDEX IF NOT EXISTS ix_posts_source_path ON scan_posts (source_path);
