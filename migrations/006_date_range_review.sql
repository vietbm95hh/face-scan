-- =====================================================================
-- 006: lọc bài theo năm/ngày + đối chiếu (review) kết quả quét với Facebook
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Khoảng thời gian lọc bài (UTC), dùng chung cho quét group, quét từ khóa và review.
--   date_from = mốc đầu (bao gồm), date_to = mốc cuối (KHÔNG bao gồm)
-- Khác `days` (N ngày gần nhất) — chỉ dùng một trong hai.
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS date_from timestamptz;
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS date_to   timestamptz;

-- Bỏ qua những gì đã xử lý (bài đã có trong DB luôn được nhận ra qua fb_post_id,
-- không bao giờ tải lại chi tiết/ảnh):
--   exclusions       : mảng fb_post_id đã review và loại trừ (không lưu lại)
--   excluded_terms   : bài chứa cụm này -> bỏ qua
--   exclude_sources  : mảng source_fb_id (Page/trang cá nhân, vd "shopabc") -> bỏ qua
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS exclusions      text[]  NOT NULL DEFAULT '{}';

-- Chỉ lưu bài có từ khóa trong tiêu đề/nội dung:
--   strict (mặc định) = đủ mọi từ quan trọng, normal = như strict,
--   loose = đủ ít nhất 1 từ, off = không kiểm tra (giữ hành vi cũ).
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS keyword_match text NOT NULL DEFAULT 'strict';

-- Thứ tự đọc kết quả tìm kiếm: recent (mặc định, bài mới trước — cần để lọc theo ngày)
-- hoặc relevance (thứ tự liên quan mặc định của Facebook).
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS sort text NOT NULL DEFAULT 'recent';
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS excluded_terms  text[]  NOT NULL DEFAULT '{}';
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS exclude_sources text[]  NOT NULL DEFAULT '{}';

-- Các năm/ngày đã quét xong cho cùng (group|từ khóa + phạm vi nguồn), để hiện
-- "đã quét năm nào" và tránh quét lại vô ích.
ALTER TABLE scan_requests ADD COLUMN IF NOT EXISTS scope_key text;

-- Kết quả đối chiếu: mỗi lần review/verify ghi 1 dòng (lịch sử).
--   new_ids     : id trong scan_posts của bài đã lưu (mới thêm khi quét)
--   existing_ids: id bài đã có trong DB (chỉ cập nhật chỉ số)
--   skipped     : bài FB trả về nhưng bị bỏ qua kèm lý do
CREATE TABLE IF NOT EXISTS scan_reviews (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id   bigint      REFERENCES scan_requests(id) ON DELETE SET NULL,
    keyword_id   bigint      REFERENCES scan_keywords(id) ON DELETE SET NULL,
    group_id     bigint      REFERENCES scan_groups(id) ON DELETE SET NULL,
    term         text,
    date_from    timestamptz,
    date_to      timestamptz,
    found        integer     NOT NULL DEFAULT 0,  -- số bài FB trả về trong khoảng (sau giới hạn max)
    in_range     integer     NOT NULL DEFAULT 0,  -- trong đó: đúng khoảng thời gian
    new_ids      bigint[]    NOT NULL DEFAULT '{}',
    existing_ids bigint[]    NOT NULL DEFAULT '{}',
    skipped      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    search_all   boolean     NOT NULL DEFAULT false,
    created_by   text        NOT NULL DEFAULT 'api',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_reviews_created ON scan_reviews (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_requests_scope  ON scan_requests (scope_key, status);
