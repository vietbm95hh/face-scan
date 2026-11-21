-- =====================================================================
-- 002: hàng đợi yêu cầu quét (API đặt yêu cầu, CLI/worker thực thi)
-- Chạy:  python -m fbscan.cli init   (idempotent, chạy lại nhiều lần OK)
-- =====================================================================

-- Một dòng = một yêu cầu "quét group X trong N ngày / tối đa M bài".
-- status: queued -> running -> done | error | canceled
CREATE TABLE IF NOT EXISTS scan_requests (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id        bigint      NOT NULL REFERENCES scan_groups(id) ON DELETE CASCADE,
    status          text        NOT NULL DEFAULT 'queued',
    max_posts       integer     NOT NULL DEFAULT 0,   -- 0 = không giới hạn
    days            integer     NOT NULL DEFAULT 0,   -- 0 = không giới hạn
    reset_cursor    boolean     NOT NULL DEFAULT false,
    download_media  boolean     NOT NULL DEFAULT true,
    requested_by    text        NOT NULL DEFAULT 'api',
    note            text,
    job_id          bigint,                          -- scan_jobs.id khi bắt đầu chạy
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    CONSTRAINT scan_requests_status_ck
        CHECK (status IN ('queued', 'running', 'done', 'error', 'canceled'))
);

CREATE INDEX IF NOT EXISTS ix_requests_status ON scan_requests (status, created_at);
CREATE INDEX IF NOT EXISTS ix_requests_group  ON scan_requests (group_id, created_at DESC);
