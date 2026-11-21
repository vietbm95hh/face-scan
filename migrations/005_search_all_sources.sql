-- =====================================================================
-- 005: quét ô tìm kiếm lấy MỌI bài (group / page / trang cá nhân)
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Nguồn của bài:
--   group  -> group_id trỏ scan_groups, source_fb_id = fb_group_id
--   page   -> source_fb_id = id/username của Page (không có trong scan_groups)
--   user   -> source_fb_id = id/username của trang cá nhân
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS source_type text NOT NULL DEFAULT 'group';
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS source_fb_id   text;
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS source_name    text;
ALTER TABLE scan_posts ADD COLUMN IF NOT EXISTS source_url     text;

-- Bài cũ (quét trước 005) đều là bài group.
UPDATE scan_posts p
SET source_type = 'group',
    source_fb_id = g.fb_group_id,
    source_name  = g.name,
    source_url   = g.group_url
FROM scan_groups g
WHERE g.id = p.group_id AND p.source_type = 'group' AND p.source_fb_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_posts_source_fbid ON scan_posts (source_fb_id);

-- Bài Page/trang cá nhân không có group_id -> dedupe theo fb_post_id.
ALTER TABLE scan_posts ALTER COLUMN group_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_fbid_no_group ON scan_posts (fb_post_id)
    WHERE group_id IS NULL;

-- Lịch sử quét theo từ khóa không thuộc group nào.
ALTER TABLE scan_jobs ALTER COLUMN group_id DROP NOT NULL;

-- Yêu cầu quét: search_all = true -> lấy mọi bài trong ô tìm kiếm (kể cả bài
-- page/trang cá nhân); false -> bỏ qua bài không thuộc group (hành vi cũ).
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS search_all boolean NOT NULL DEFAULT false;
