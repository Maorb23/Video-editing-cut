-- Accounts and sessions are managed by Django migrations. Existing service
-- records deliberately remain unowned; new records store the Django user ID.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE edits ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE iterations ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS user_id text;
ALTER TABLE results ADD COLUMN IF NOT EXISTS user_id text;
CREATE INDEX IF NOT EXISTS videos_user_id ON videos(user_id);
CREATE INDEX IF NOT EXISTS edits_user_id ON edits(user_id);
CREATE INDEX IF NOT EXISTS plans_user_id ON plans(user_id);
CREATE INDEX IF NOT EXISTS iterations_user_id ON iterations(user_id);
CREATE INDEX IF NOT EXISTS artifacts_user_id ON artifacts(user_id);
CREATE INDEX IF NOT EXISTS approvals_user_id ON approvals(user_id);
CREATE INDEX IF NOT EXISTS results_user_id ON results(user_id);
