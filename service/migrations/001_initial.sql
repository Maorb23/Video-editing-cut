CREATE TABLE IF NOT EXISTS videos (
    id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('uploaded', 'failed')),
    filename text NOT NULL,
    content_type text,
    storage_key text NOT NULL UNIQUE,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edits (
    id text PRIMARY KEY,
    video_id text NOT NULL REFERENCES videos(id),
    instruction text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'uploaded', 'analyzing', 'planning', 'awaiting_approval',
        'approved', 'rendering', 'completed', 'failed'
    )),
    progress jsonb,
    failure jsonb,
    accepted_artifact_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plans (
    id text PRIMARY KEY,
    edit_id text NOT NULL REFERENCES edits(id),
    status text NOT NULL CHECK (status IN ('proposed', 'approved')),
    summary text NOT NULL,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    document jsonb NOT NULL,
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    workspace_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_plan_per_edit ON plans(edit_id);

CREATE TABLE IF NOT EXISTS approvals (
    id text PRIMARY KEY,
    edit_id text NOT NULL REFERENCES edits(id),
    plan_id text NOT NULL REFERENCES plans(id),
    plan_sha256 text NOT NULL CHECK (length(plan_sha256) = 64),
    approved_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(edit_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    edit_id text NOT NULL REFERENCES edits(id),
    kind text NOT NULL CHECK (kind IN ('plan', 'render')),
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    claimed_by text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(edit_id, kind)
);
CREATE INDEX IF NOT EXISTS claimable_jobs ON jobs(status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id text PRIMARY KEY,
    edit_id text NOT NULL REFERENCES edits(id),
    kind text NOT NULL,
    storage_key text NOT NULL UNIQUE,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    accepted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(edit_id, kind)
);

ALTER TABLE edits DROP CONSTRAINT IF EXISTS edits_accepted_artifact_id_fkey;
ALTER TABLE edits ADD CONSTRAINT edits_accepted_artifact_id_fkey
    FOREIGN KEY (accepted_artifact_id) REFERENCES artifacts(id);

CREATE TABLE IF NOT EXISTS results (
    id text PRIMARY KEY,
    edit_id text NOT NULL UNIQUE REFERENCES edits(id),
    accepted_artifact_id text NOT NULL UNIQUE REFERENCES artifacts(id),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
