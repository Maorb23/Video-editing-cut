CREATE FUNCTION protect_artifact_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'artifact records are immutable';
END $$;
CREATE TRIGGER immutable_artifact BEFORE UPDATE ON artifacts FOR EACH ROW EXECUTE FUNCTION protect_artifact_content();
