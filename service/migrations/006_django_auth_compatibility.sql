-- Early development builds attached ownership columns to a custom users table.
-- Django now owns identity, and service tables retain its user ID as text.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT source.relname AS table_name, constraint_row.conname AS constraint_name
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS source ON source.oid = constraint_row.conrelid
        JOIN pg_class AS target ON target.oid = constraint_row.confrelid
        JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace
        WHERE constraint_row.contype = 'f'
          AND namespace.nspname = current_schema()
          AND target.relname = 'users'
          AND source.relname IN ('videos', 'edits', 'plans', 'iterations', 'artifacts', 'approvals', 'results')
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', item.table_name, item.constraint_name);
    END LOOP;
END $$;
