CREATE TABLE iterations (
    edit_id text NOT NULL REFERENCES edits(id),
    iteration integer NOT NULL CHECK (iteration > 0),
    parent_iteration integer,
    instruction text NOT NULL,
    status text NOT NULL DEFAULT 'planning' CHECK (status IN ('planning','awaiting_approval','approved','rendering','completed','failed')),
    preview_status text NOT NULL DEFAULT 'queued' CHECK (preview_status IN ('queued','running','succeeded','failed')),
    render_status text CHECK (render_status IN ('queued','running','succeeded','failed')),
    artifact_paths jsonb NOT NULL DEFAULT '{}',
    failure jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (edit_id, iteration),
    FOREIGN KEY (edit_id, parent_iteration) REFERENCES iterations(edit_id, iteration),
    CHECK (parent_iteration IS NULL OR parent_iteration < iteration)
);
ALTER TABLE edits ADD COLUMN current_iteration integer NOT NULL DEFAULT 1;
ALTER TABLE edits ADD COLUMN active_iteration integer;
ALTER TABLE edits ADD COLUMN approved_iteration integer;
INSERT INTO iterations(edit_id,iteration,instruction,status,preview_status,render_status)
SELECT id,1,instruction,CASE WHEN state IN ('uploaded','analyzing') THEN 'planning' ELSE state END,
    'queued',CASE WHEN state='completed' THEN 'succeeded' ELSE NULL END FROM edits;
UPDATE edits SET active_iteration=1 WHERE state='completed';
UPDATE edits SET approved_iteration=1 WHERE state IN ('approved','rendering','completed');
DROP INDEX one_plan_per_edit;
ALTER TABLE plans ADD COLUMN iteration integer NOT NULL DEFAULT 1;
ALTER TABLE plans ADD COLUMN decision_log jsonb NOT NULL DEFAULT '{"observations":[],"decisions":[],"unsupported":[],"assumptions":[]}';
ALTER TABLE plans ADD UNIQUE(edit_id,iteration);
ALTER TABLE plans ADD FOREIGN KEY(edit_id,iteration) REFERENCES iterations(edit_id,iteration);
ALTER TABLE approvals DROP CONSTRAINT approvals_edit_id_key;
ALTER TABLE approvals ADD UNIQUE(plan_id);
ALTER TABLE jobs DROP CONSTRAINT jobs_edit_id_kind_key;
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD COLUMN iteration integer NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN progress jsonb;
ALTER TABLE jobs ADD CHECK (kind IN ('plan','revision','compilation','preview','inspection','render'));
ALTER TABLE jobs ADD UNIQUE(edit_id,iteration,kind);
ALTER TABLE jobs ADD FOREIGN KEY(edit_id,iteration) REFERENCES iterations(edit_id,iteration);
ALTER TABLE artifacts DROP CONSTRAINT artifacts_edit_id_kind_key;
ALTER TABLE artifacts ADD COLUMN iteration integer NOT NULL DEFAULT 1;
ALTER TABLE artifacts ADD UNIQUE(edit_id,iteration,kind);
ALTER TABLE artifacts ADD FOREIGN KEY(edit_id,iteration) REFERENCES iterations(edit_id,iteration);
ALTER TABLE results DROP CONSTRAINT results_edit_id_key;
ALTER TABLE results ADD COLUMN iteration integer NOT NULL DEFAULT 1;
ALTER TABLE results ADD UNIQUE(edit_id,iteration);
ALTER TABLE results ADD FOREIGN KEY(edit_id,iteration) REFERENCES iterations(edit_id,iteration);

-- Content and identity are immutable; lifecycle fields may advance.
CREATE FUNCTION protect_plan_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.document,NEW.sha256,NEW.decision_log,NEW.edit_id,NEW.iteration,NEW.workspace_key,NEW.summary,NEW.warnings)
     IS DISTINCT FROM (OLD.document,OLD.sha256,OLD.decision_log,OLD.edit_id,OLD.iteration,OLD.workspace_key,OLD.summary,OLD.warnings) THEN
    RAISE EXCEPTION 'plan content is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_plan BEFORE UPDATE ON plans FOR EACH ROW EXECUTE FUNCTION protect_plan_content();
CREATE FUNCTION protect_iteration_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.edit_id,NEW.iteration,NEW.parent_iteration,NEW.instruction) IS DISTINCT FROM
     (OLD.edit_id,OLD.iteration,OLD.parent_iteration,OLD.instruction) THEN
    RAISE EXCEPTION 'iteration identity is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_iteration BEFORE UPDATE ON iterations FOR EACH ROW EXECUTE FUNCTION protect_iteration_identity();
