-- =====================================================================
-- 003: thao tác quản trị cho UI (xoá bài, bật/tắt group, đánh dấu đã đọc)
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Đánh dấu bài đã đọc / lưu ý trên UI (không ảnh hưởng dữ liệu scraper).
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS is_read   boolean NOT NULL DEFAULT false;
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS note      text;
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS updated_by text;

CREATE INDEX IF NOT EXISTS ix_posts_is_read ON scan_posts (is_read) WHERE is_read = false;
