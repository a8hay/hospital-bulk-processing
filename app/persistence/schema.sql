-- Applied on startup. Every statement must be idempotent.

CREATE TABLE IF NOT EXISTS batch_job (
    batch_id          UUID PRIMARY KEY,
    status            TEXT NOT NULL CHECK (status IN ('processing', 'activating', 'completed',
                                                      'failed', 'activation_failed', 'interrupted')),
    total_rows        INT  NOT NULL CHECK (total_rows >= 1),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    activated_at      TIMESTAMPTZ,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error        TEXT
);

-- the sweeper only ever looks at batches a runner should currently own
CREATE INDEX IF NOT EXISTS batch_job_running_heartbeat
    ON batch_job (last_heartbeat_at) WHERE status IN ('processing', 'activating');

CREATE TABLE IF NOT EXISTS batch_job_row (
    batch_id             UUID NOT NULL REFERENCES batch_job (batch_id) ON DELETE CASCADE,
    row_no               INT  NOT NULL CHECK (row_no >= 1),
    name                 TEXT NOT NULL,
    address              TEXT NOT NULL,
    phone                TEXT,
    status               TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'created',
                                                         'unknown', 'retry_exhausted', 'rejected')),
    upstream_hospital_id INT,
    attempts             INT  NOT NULL DEFAULT 0,
    last_error           TEXT,
    started_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    PRIMARY KEY (batch_id, row_no),
    -- a row is created exactly when we know which upstream hospital it became
    CHECK ((status = 'created') = (upstream_hospital_id IS NOT NULL))
);
