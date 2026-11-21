-- =====================================================================
-- fbscan - schema kho dữ liệu bài viết Facebook Group
-- Chạy:  psql -h 127.0.0.1 -p 2024 -U postgres -d fbscan -f migrations/001_init.sql
-- Yêu cầu: PostgreSQL >= 13 (pg_trgm)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- groups
-- Danh sách group được quét. Dòng được tạo bởi discover.py (Playwright)
-- hoặc thủ công khi truyền --group.
CREATE TABLE IF NOT EXISTS scan_groups (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fb_group_id       text        NOT NULL UNIQUE,
    slug              text,
    name              text,
    privacy           text,
    member_count_txt  text,
    group_url         text,
    avatar_path       text,
    is_active         boolean     NOT NULL DEFAULT true,
    discovered_at     timestamptz NOT NULL DEFAULT now(),
    last_scanned_at   timestamptz
);

-- ---------------------------------------------------------------- posts
-- Mỗi bài viết trong group. Khóa tự nhiên = (group_id, fb_post_id).
-- content giữ nguyên văn bản bài; tiêu đề FB thường rỗng nên lấy text
-- làm title fallback khi hiển thị (xem api/main.py).
CREATE TABLE IF NOT EXISTS scan_posts (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fb_post_id         text        NOT NULL,
    group_id           bigint      NOT NULL REFERENCES scan_groups(id) ON DELETE CASCADE,
    author_fb_id       text,
    author_name        text,
    title              text,
    content            text,
    permalink          text,
    posted_at          timestamptz,
    like_count         integer     NOT NULL DEFAULT 0,
    comment_count      integer     NOT NULL DEFAULT 0,
    share_count        integer     NOT NULL DEFAULT 0,
    reaction_breakdown jsonb       NOT NULL DEFAULT '[]'::jsonb,
    has_media          boolean     NOT NULL DEFAULT false,
    scraped_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    raw                jsonb,
    CONSTRAINT scan_posts_group_post_uk UNIQUE (group_id, fb_post_id)
);

-- ----------------------------------------------------------------- media
-- Ảnh/video đính kèm. file_path là đường dẫn tương đối trong MEDIA_DIR.
CREATE TABLE IF NOT EXISTS scan_post_media (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id       bigint      NOT NULL REFERENCES scan_posts(id) ON DELETE CASCADE,
    kind          text        NOT NULL DEFAULT 'photo',   -- photo | video | generic | sticker
    position      integer     NOT NULL DEFAULT 0,
    source_url    text,
    file_path     text,
    thumb_url     text,
    width         integer,
    height        integer,
    bytes         bigint,
    downloaded_at timestamptz
);

-- ------------------------------------------------------------------ jobs
-- Lịch sử mỗi lần quét (phục vụ API / trạng thái CLI).
CREATE TABLE IF NOT EXISTS scan_jobs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id       bigint REFERENCES scan_groups(id) ON DELETE SET NULL,
    status         text        NOT NULL DEFAULT 'queued', -- queued|running|done|error
    trigger_source text        NOT NULL DEFAULT 'cli',    -- cli|api|schedule
    started_at     timestamptz,
    finished_at    timestamptz,
    posts_found    integer     NOT NULL DEFAULT 0,
    posts_new      integer     NOT NULL DEFAULT 0,
    posts_updated  integer     NOT NULL DEFAULT 0,
    error          text
);

-- --------------------------------------------------------------- cursors
-- Trạng thái phân trang đã lưu (freeze/thaw của PaginatedIterator).
CREATE TABLE IF NOT EXISTS scan_cursors (
    group_id   bigint      PRIMARY KEY REFERENCES scan_groups(id) ON DELETE CASCADE,
    cursor     text,
    has_next   boolean     NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------- indexes
CREATE INDEX IF NOT EXISTS ix_posts_group_time   ON scan_posts (group_id, posted_at DESC);
CREATE INDEX IF NOT EXISTS ix_posts_posted       ON scan_posts (posted_at DESC);
CREATE INDEX IF NOT EXISTS ix_posts_author       ON scan_posts (author_fb_id);
CREATE INDEX IF NOT EXISTS ix_posts_content_trgm ON scan_posts USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_media_post         ON scan_post_media (post_id, position);
CREATE INDEX IF NOT EXISTS ix_jobs_status        ON scan_jobs (status, started_at DESC);
