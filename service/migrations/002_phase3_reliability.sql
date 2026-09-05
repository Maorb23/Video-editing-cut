ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz;

CREATE TABLE IF NOT EXISTS job_attempts (
    job_id text NOT NULL REFERENCES jobs(id),
    attempt integer NOT NULL CHECK (attempt > 0),
    worker_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'retrying', 'succeeded', 'failed', 'lost')),
    error jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY(job_id, attempt)
);

CREATE INDEX IF NOT EXISTS job_attempts_status ON job_attempts(status, started_at);
