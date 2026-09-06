ALTER TABLE edits
    ADD COLUMN billing_exempt boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN edits.billing_exempt IS
    'Immutable per-edit billing policy snapshot; staff-created edits are unmetered.';
