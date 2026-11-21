-- =====================================================================
-- 004: quét theo từ khóa (ô tìm kiếm Facebook)
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Từ khóa tìm kiếm. Một từ khóa có thể được dùng cho nhiều yêu cầu quét.
CREATE TABLE IF NOT EXISTS scan_keywords (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    term        text        NOT NULL,
    term_key    text        NOT NULL UNIQUE,      -- term đã chuẩn hoá (lower + gộp khoảng trắng)
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Yêu cầu quét: giờ có thể quét theo group (keyword_id NULL)
-- hoặc quét theo từ khóa trong ô tìm kiếm Facebook (keyword_id có, group_id NULL).
ALTER TABLE scan_requests ALTER COLUMN group_id DROP NOT NULL;
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS keyword_id bigint
    REFERENCES scan_keywords(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_requests_keyword ON scan_requests (keyword_id, created_at DESC);

-- Bài tìm được qua ô tìm kiếm -> trỏ về từ khóa. NULL = bài chỉ có từ feed group.
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS keyword_id bigint
    REFERENCES scan_keywords(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_posts_keyword ON scan_posts (keyword_id, posted_at DESC)
    WHERE keyword_id IS NOT NULL;
