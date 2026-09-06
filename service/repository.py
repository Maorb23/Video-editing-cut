from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    edit_id: str
    kind: str
    video_id: str
    instruction: str
    storage_key: str
    filename: str
    claimed_by: str
    attempts: int
    iteration: int = 1
    parent_iteration: int | None = None
    source_sha256: str = ""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PostgresRepository:
    def update_plan_progress(self, job: ClaimedJob, message: str) -> None:
        stage = "dereverb" if message == "Cleaning room echo" else "planning"
        progress = json.dumps({"stage": stage, "message": message, "status": "running",
                               "iteration": job.iteration, "percent": 30 if stage == "dereverb" else 40})
        with self._connect() as connection:
            active = connection.execute(
                "UPDATE jobs SET progress=%s,updated_at=now() WHERE id=%s AND status='running' AND attempts=%s AND claimed_by=%s RETURNING id",
                (progress, job.id, job.attempts, job.claimed_by),
            ).fetchone()
            if active:
                connection.execute(
                    "UPDATE edits SET progress=%s,updated_at=now() WHERE id=%s AND current_iteration=%s",
                    (progress, job.edit_id, job.iteration),
                )

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires the 'service' project extra") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.database_url = database_url

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def migrate(self) -> None:
        migrations = Path(__file__).with_name("migrations")
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(730019)")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            for migration in sorted(migrations.glob("*.sql")):
                exists = connection.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (migration.name,)).fetchone()
                if exists:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (migration.name,))

    def create_video(self, *, filename: str, content_type: str | None, storage_key: str, size: int, sha256: str, video_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        video_id = video_id or new_id("vid")
        with self._connect() as connection:
            return connection.execute(
                "INSERT INTO videos(id,state,filename,content_type,storage_key,size_bytes,sha256,user_id) VALUES (%s,'uploaded',%s,%s,%s,%s,%s,%s) RETURNING *",
                (video_id, filename, content_type, storage_key, size, sha256, user_id),
            ).fetchone()

    def ensure_profile(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "INSERT INTO user_profiles(user_id) VALUES (%s) ON CONFLICT(user_id) DO UPDATE SET user_id=excluded.user_id RETURNING *",
                (user_id,),
            ).fetchone()
            exists = connection.execute("SELECT 1 FROM credit_ledger_entries WHERE user_id=%s", (user_id,)).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO credit_ledger_entries(id,user_id,amount,reason,idempotency_key,metadata) VALUES (%s,%s,200,'promotion','welcome',%s)",
                    (new_id("crd"), user_id, json.dumps({"campaign": "welcome"})),
                )
            return row

    def get_profile(self, user_id: str) -> dict[str, Any]:
        self.ensure_profile(user_id)
        with self._connect() as connection:
            return connection.execute("SELECT * FROM user_profiles WHERE user_id=%s", (user_id,)).fetchone()

    def set_avatar(self, user_id: str, avatar_key: str) -> dict[str, Any]:
        self.ensure_profile(user_id)
        with self._connect() as connection:
            return connection.execute("UPDATE user_profiles SET avatar_key=%s,updated_at=now() WHERE user_id=%s RETURNING *", (avatar_key, user_id)).fetchone()

    def verify_email(self, user_id: str) -> dict[str, Any]:
        self.ensure_profile(user_id)
        with self._connect() as connection:
            return connection.execute("UPDATE user_profiles SET email_verified_at=COALESCE(email_verified_at,now()),updated_at=now() WHERE user_id=%s RETURNING *", (user_id,)).fetchone()

    def credit_summary(self, user_id: str) -> dict[str, Any]:
        self.ensure_profile(user_id)
        with self._connect() as connection:
            entries = connection.execute("SELECT id,amount,reason,edit_id,payment_reference,metadata,created_at FROM credit_ledger_entries WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 100", (user_id,)).fetchall()
            balance = connection.execute("SELECT COALESCE(sum(amount),0)::integer AS balance FROM credit_ledger_entries WHERE user_id=%s", (user_id,)).fetchone()["balance"]
        return {"balance": balance, "entries": entries}

    def create_mock_top_up(self, user_id: str, package: dict[str, Any], succeed: bool) -> dict[str, Any]:
        order_id = new_id("ord")
        status = "succeeded" if succeed else "failed"
        with self._connect() as connection:
            order = connection.execute(
                "INSERT INTO top_up_orders(id,user_id,package_key,credits,price_minor,currency,status,failure_code,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *",
                (order_id, user_id, package["key"], package["credits"], package["price_minor"], package["currency"], status, None if succeed else "mock_declined"),
            ).fetchone()
            if succeed:
                connection.execute(
                    "INSERT INTO credit_ledger_entries(id,user_id,amount,reason,payment_reference,idempotency_key,metadata) VALUES (%s,%s,%s,'purchase',%s,%s,%s)",
                    (new_id("crd"), user_id, package["credits"], order_id, f"mock-order:{order_id}", json.dumps({"package_key": package["key"], "provider": "mock"})),
                )
            return order

    def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT e.id,e.video_id,e.instruction,e.state,e.created_at,e.updated_at,e.current_iteration,
                          e.active_iteration,e.approved_iteration,v.filename,
                          COALESCE((SELECT sum(-amount) FROM credit_ledger_entries l WHERE l.edit_id=e.id AND l.amount<0),0)::integer AS credits_used
                   FROM edits e JOIN videos v ON v.id=e.video_id WHERE e.user_id=%s
                   ORDER BY e.updated_at DESC,e.id DESC""", (user_id,),
            ).fetchall()
            for row in rows:
                row["iterations"] = connection.execute(
                    """SELECT i.iteration,i.parent_iteration,i.instruction,i.status,i.preview_status,i.render_status,i.created_at,
                              EXISTS(SELECT 1 FROM artifacts a WHERE a.edit_id=i.edit_id AND a.iteration=i.iteration AND a.kind='poster') AS has_poster,
                              EXISTS(SELECT 1 FROM artifacts a WHERE a.edit_id=i.edit_id AND a.iteration=i.iteration AND a.kind='video') AS has_video
                       FROM iterations i WHERE i.edit_id=%s ORDER BY i.iteration""", (row["id"],),
                ).fetchall()
        return rows

    def create_edit(self, *, video_id: str, instruction: str, user_id: str | None = None, billing_exempt: bool = False) -> dict[str, Any]:
        edit_id, job_id = new_id("edt"), new_id("job")
        with self._connect() as connection:
            if not connection.execute("SELECT 1 FROM videos WHERE id=%s AND state='uploaded' AND (%s::text IS NULL OR user_id=%s)", (video_id, user_id, user_id)).fetchone():
                raise NotFoundError("video not found")
            edit = connection.execute(
                "INSERT INTO edits(id,video_id,instruction,state,progress,user_id,billing_exempt) VALUES (%s,%s,%s,'analyzing',%s,%s,%s) RETURNING *",
                (edit_id, video_id, instruction, json.dumps({"stage": "queued"}), user_id, billing_exempt),
            ).fetchone()
            connection.execute("INSERT INTO iterations(edit_id,iteration,instruction,user_id) VALUES (%s,1,%s,%s)", (edit_id, instruction, user_id))
            connection.execute("INSERT INTO jobs(id,edit_id,kind,status) VALUES (%s,%s,'plan','queued')", (job_id, edit_id))
            return edit

    def get_edit(self, edit_id: str, user_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT e.*,i.preview_status,i.render_status FROM edits e JOIN iterations i ON i.edit_id=e.id AND i.iteration=e.current_iteration WHERE e.id=%s AND (%s::text IS NULL OR e.user_id=%s)", (edit_id, user_id, user_id)).fetchone()
            if row:
                row["jobs"] = connection.execute("SELECT id,iteration,kind,status,progress,attempts,last_error,created_at,started_at,finished_at FROM jobs WHERE edit_id=%s ORDER BY created_at,id", (edit_id,)).fetchall()
        if not row:
            raise NotFoundError("edit not found")
        return row

    def get_plan(self, edit_id: str, iteration: int | None = None, user_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT p.*,i.parent_iteration,i.instruction,i.preview_status,i.render_status,i.status AS plan_status,i.failure FROM plans p JOIN iterations i USING(edit_id,iteration) JOIN edits e ON e.id=p.edit_id WHERE p.edit_id=%s AND p.iteration=COALESCE(%s,e.current_iteration) AND (%s::text IS NULL OR e.user_id=%s)", (edit_id, iteration, user_id, user_id)).fetchone()
        if not row:
            raise NotFoundError("plan not available")
        return row

    def approve(self, edit_id: str, plan_id: str, user_id: str | None = None) -> dict[str, Any]:
        approval_id = new_id("apr")
        with self._connect() as connection:
            edit = connection.execute("SELECT * FROM edits WHERE id=%s AND (%s::text IS NULL OR user_id=%s) FOR UPDATE", (edit_id, user_id, user_id)).fetchone()
            plan = connection.execute("SELECT p.*,i.status AS iteration_status,i.preview_status FROM plans p JOIN iterations i USING(edit_id,iteration) WHERE p.id=%s AND p.edit_id=%s", (plan_id, edit_id)).fetchone()
            if not edit or not plan:
                raise NotFoundError("edit or plan not found")
            if edit["state"] in {"analyzing", "planning", "rendering"} or plan["iteration_status"] not in {"awaiting_approval", "approved", "completed"}:
                raise ConflictError("a compiled, reviewable iteration is required")
            if canonical_digest(plan["document"]) != plan["sha256"]:
                raise ConflictError("stored plan digest does not match its document")
            connection.execute(
                "INSERT INTO approvals(id,edit_id,plan_id,plan_sha256,user_id) VALUES (%s,%s,%s,%s,%s) ON CONFLICT(plan_id) DO NOTHING",
                (approval_id, edit_id, plan_id, plan["sha256"], user_id),
            )
            connection.execute("UPDATE plans SET status='approved' WHERE id=%s", (plan_id,))
            connection.execute("UPDATE iterations SET status=CASE WHEN status='completed' THEN status ELSE 'approved' END,updated_at=now() WHERE edit_id=%s AND iteration=%s", (edit_id, plan["iteration"]))
            return connection.execute(
                "UPDATE edits SET state='approved',approved_iteration=%s,progress=%s,updated_at=now() WHERE id=%s RETURNING *",
                (plan["iteration"], json.dumps({"stage": "approved", "iteration": plan["iteration"]}), edit_id),
            ).fetchone()

    def queue_render(self, edit_id: str, user_id: str | None = None) -> dict[str, Any]:
        job_id = new_id("job")
        with self._connect() as connection:
            edit = connection.execute("SELECT * FROM edits WHERE id=%s AND (%s::text IS NULL OR user_id=%s) FOR UPDATE", (edit_id, user_id, user_id)).fetchone()
            if not edit:
                raise NotFoundError("edit not found")
            if edit["state"] != "approved":
                raise ConflictError("an approved plan is required before rendering")
            approval = connection.execute(
                "SELECT 1 FROM approvals a JOIN plans p ON p.id=a.plan_id WHERE a.edit_id=%s AND p.iteration=%s AND a.plan_sha256=p.sha256",
                (edit_id, edit["approved_iteration"]),
            ).fetchone()
            if not approval:
                raise ConflictError("approved plan digest does not match")
            if connection.execute("SELECT 1 FROM jobs WHERE edit_id=%s AND iteration=%s AND kind='render'", (edit_id, edit["approved_iteration"])).fetchone():
                raise ConflictError("render is already queued")
            connection.execute("INSERT INTO jobs(id,edit_id,iteration,kind,status) VALUES (%s,%s,%s,'render','queued')", (job_id, edit_id, edit["approved_iteration"]))
            connection.execute("UPDATE iterations SET render_status='queued',updated_at=now() WHERE edit_id=%s AND iteration=%s", (edit_id, edit["approved_iteration"]))
            return connection.execute("UPDATE edits SET state='rendering',progress=%s,updated_at=now() WHERE id=%s RETURNING *", (json.dumps({"stage": "render", "status": "queued", "iteration": edit["approved_iteration"]}), edit_id)).fetchone()

    def claim(self, worker_id: str, lease_seconds: int, max_attempts: int = 3) -> ClaimedJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT j.id FROM jobs j
                    WHERE (j.status='queued' OR (j.status='running' AND j.lease_until < now()))
                      AND j.attempts < %s
                    ORDER BY j.created_at, j.id FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE jobs j SET status='running',claimed_by=%s,started_at=now(),finished_at=NULL,
                    lease_until=now()+(%s * interval '1 second'),attempts=j.attempts+1,updated_at=now()
                FROM candidate c, edits e, videos v, iterations i
                WHERE j.id=c.id AND e.id=j.edit_id AND v.id=e.video_id AND i.edit_id=e.id AND i.iteration=j.iteration
                RETURNING j.id,j.edit_id,j.kind,e.video_id,i.instruction,v.storage_key,v.filename,j.claimed_by,j.attempts,j.iteration,i.parent_iteration,v.sha256 AS source_sha256
                """,
                (max_attempts, worker_id, lease_seconds),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE job_attempts SET status='lost',error=%s,finished_at=now() WHERE job_id=%s AND status='running'",
                (json.dumps({"code": "worker_lost", "retryable": True}), row["id"]),
            )
            self._progress(connection, ClaimedJob(**row), "running")
            connection.execute(
                "INSERT INTO job_attempts(job_id,attempt,worker_id,status) VALUES (%s,%s,%s,'running')",
                (row["id"], row["attempts"], worker_id),
            )
            return ClaimedJob(**row)

    def reconcile_exhausted(self, max_attempts: int) -> int:
        error = json.dumps({
            "code": "worker_lost", "stage": "worker", "message": "worker lease expired at attempt limit",
            "retryable": False,
        })
        with self._connect() as connection:
            rows = connection.execute(
                """
                UPDATE jobs SET status='failed',last_error=%s,finished_at=now(),lease_until=NULL,updated_at=now()
                WHERE status='running' AND lease_until < now() AND attempts >= %s
                RETURNING id,edit_id,attempts,iteration,kind
                """,
                (error, max_attempts),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE job_attempts SET status='lost',error=%s,finished_at=now() WHERE job_id=%s AND attempt=%s",
                    (error, row["id"], row["attempts"]),
                )
                self._failure(connection, row["edit_id"], row["iteration"], row["kind"], json.loads(error))
        return len(rows)

    def retry(self, job: ClaimedJob, error: dict[str, Any]) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE jobs SET status='queued',claimed_by=NULL,lease_until=NULL,last_error=%s,updated_at=now()
                WHERE id=%s AND status='running' AND claimed_by=%s AND attempts=%s AND lease_until>now() RETURNING edit_id,kind
                """,
                (json.dumps(error), job.id, job.claimed_by, job.attempts),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                "UPDATE job_attempts SET status='retrying',error=%s,finished_at=now() WHERE job_id=%s AND attempt=%s",
                (json.dumps(error), job.id, job.attempts),
            )
            self._progress(connection, job, "queued")
        return True

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE jobs SET lease_until=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s AND claimed_by=%s AND status='running' AND lease_until>now() RETURNING id",
                (lease_seconds, job_id, worker_id),
            ).fetchone()
        return bool(row)

    def complete_plan(self, job: ClaimedJob, *, summary: str, document: dict[str, Any], warnings: list[str], workspace_key: str, decision_log: dict[str, Any] | None = None) -> str:
        plan_id = new_id("pln")
        digest = canonical_digest(document)
        with self._connect() as connection:
            self._owned(connection, job)
            locked = connection.execute("SELECT state FROM edits WHERE id=%s FOR UPDATE", (job.edit_id,)).fetchone()
            if not locked or locked["state"] != "planning":
                raise ConflictError("planning job no longer owns the edit state")
            connection.execute(
                "INSERT INTO plans(id,edit_id,iteration,status,summary,warnings,document,sha256,workspace_key,decision_log,user_id) SELECT %s,%s,%s,'proposed',%s,%s,%s,%s,%s,%s,user_id FROM edits WHERE id=%s",
                (plan_id, job.edit_id, job.iteration, summary, json.dumps(warnings), json.dumps(document), digest, workspace_key, json.dumps(decision_log or {"observations": [], "decisions": [], "unsupported": [], "assumptions": []}), job.edit_id),
            )
            self._finish(connection, job)
            connection.execute("UPDATE iterations SET artifact_paths=%s,updated_at=now() WHERE edit_id=%s AND iteration=%s", (json.dumps({"workspace": workspace_key, "plan": f"{workspace_key}/edit-plan.json", "decisions": f"{workspace_key}/decisions.json"}), job.edit_id, job.iteration))
            self._queue(connection, job.edit_id, job.iteration, "compilation")
        return plan_id

    def complete_render(self, job: ClaimedJob, artifacts: list[dict[str, Any]], accepted_kind: str = "video") -> None:
        preview = job.kind == "inspection"
        with self._connect() as c:
            self._owned(c, job)
            edit = c.execute("SELECT * FROM edits WHERE id=%s FOR UPDATE", (job.edit_id,)).fetchone()
            accepted_id = None
            paths = {}
            for artifact in artifacts:
                artifact_id = new_id("art")
                accepted = not preview and artifact["kind"] == accepted_kind
                c.execute("INSERT INTO artifacts(id,edit_id,iteration,kind,storage_key,size_bytes,sha256,metadata,accepted,user_id) SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,user_id FROM edits WHERE id=%s",
                          (artifact_id, job.edit_id, job.iteration, artifact["kind"], artifact["storage_key"], artifact["size"], artifact["sha256"], json.dumps(artifact.get("metadata", {})), accepted, job.edit_id))
                paths[f"storage_{artifact['kind']}"] = artifact["storage_key"]
                if accepted:
                    if accepted_id is not None:
                        raise ConflictError("render produced more than one accepted output")
                    accepted_id = artifact_id
            if not preview and accepted_id is None:
                raise ConflictError("render produced no accepted output")
            if not preview:
                c.execute("INSERT INTO results(id,edit_id,iteration,accepted_artifact_id,metadata,user_id) SELECT %s,%s,%s,%s,%s,user_id FROM edits WHERE id=%s", (new_id("res"), job.edit_id, job.iteration, accepted_id, json.dumps({"artifact_count": len(artifacts)}), job.edit_id))
                c.execute(
                    """INSERT INTO credit_ledger_entries(id,user_id,amount,reason,edit_id,idempotency_key,metadata)
                       SELECT %s,user_id,-25,'generation',id,%s,%s FROM edits
                       WHERE id=%s AND user_id IS NOT NULL AND billing_exempt=false
                       ON CONFLICT(user_id,idempotency_key) DO NOTHING""",
                    (new_id("crd"), f"generation:{job.edit_id}:{job.iteration}", json.dumps({"iteration": job.iteration}), job.edit_id),
                )
                c.execute("UPDATE iterations SET status='completed',render_status='succeeded' WHERE edit_id=%s AND iteration=%s", (job.edit_id, job.iteration))
                c.execute("UPDATE edits SET state='completed',accepted_artifact_id=%s,active_iteration=%s,progress=%s,updated_at=now() WHERE id=%s AND approved_iteration=%s", (accepted_id, job.iteration, json.dumps({"stage": "completed", "iteration": job.iteration}), job.edit_id, job.iteration))
            else:
                c.execute("UPDATE iterations SET preview_status='succeeded' WHERE edit_id=%s AND iteration=%s", (job.edit_id, job.iteration))
                if edit["active_iteration"] is None or job.iteration > edit["active_iteration"]:
                    c.execute("UPDATE edits SET active_iteration=%s,updated_at=now() WHERE id=%s", (job.iteration, job.edit_id))
            c.execute("UPDATE iterations SET artifact_paths=artifact_paths || %s::jsonb,updated_at=now() WHERE edit_id=%s AND iteration=%s", (json.dumps(paths), job.edit_id, job.iteration))
            self._finish(c, job)

    def fail(self, job: ClaimedJob, error: dict[str, Any]) -> None:
        with self._connect() as connection:
            owned = connection.execute(
                "UPDATE jobs SET status='failed',last_error=%s,lease_until=NULL,finished_at=now(),updated_at=now() WHERE id=%s AND status='running' AND claimed_by=%s AND attempts=%s AND lease_until>now() RETURNING id",
                (json.dumps(error), job.id, job.claimed_by, job.attempts),
            ).fetchone()
            if owned:
                connection.execute("UPDATE job_attempts SET status='failed',error=%s,finished_at=now() WHERE job_id=%s AND attempt=%s", (json.dumps(error), job.id, job.attempts))
                self._failure(connection, job.edit_id, job.iteration, job.kind, error)

    def get_result(self, edit_id: str, user_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._connect() as connection:
            edit = connection.execute("SELECT * FROM edits WHERE id=%s AND (%s::text IS NULL OR user_id=%s)", (edit_id, user_id, user_id)).fetchone()
            if not edit:
                raise NotFoundError("edit not found")
            if not edit["accepted_artifact_id"]:
                raise ConflictError("validated result is not available")
            result = connection.execute(
                "SELECT * FROM results WHERE edit_id=%s AND accepted_artifact_id=%s",
                (edit_id, edit["accepted_artifact_id"]),
            ).fetchone()
            if not result:
                raise ConflictError("result record does not match the accepted output")
            accepted = connection.execute(
                "SELECT * FROM artifacts WHERE id=%s AND edit_id=%s AND kind='video' AND accepted=true",
                (edit["accepted_artifact_id"], edit_id),
            ).fetchone()
            if not accepted:
                raise ConflictError("accepted output pointer is invalid")
            artifacts = connection.execute("SELECT * FROM artifacts WHERE edit_id=%s AND iteration=%s AND kind NOT IN ('preview','poster','inspection') ORDER BY kind", (edit_id, result["iteration"])).fetchall()
            if sum(1 for item in artifacts if item["accepted"]) != 1:
                raise ConflictError("result does not have exactly one accepted output")
            return edit, list(artifacts)

    def get_artifact(self, edit_id: str, kind: str, iteration: int | None = None, user_id: str | None = None) -> dict[str, Any]:
        if iteration is not None:
            with self._connect() as connection:
                row = connection.execute("SELECT a.* FROM artifacts a JOIN edits e ON e.id=a.edit_id WHERE a.edit_id=%s AND a.iteration=%s AND a.kind=%s AND (%s::text IS NULL OR e.user_id=%s)", (edit_id, iteration, kind, user_id, user_id)).fetchone()
            if not row:
                raise NotFoundError("artifact not available")
            return row
        edit, artifacts = self.get_result(edit_id, user_id)
        del edit
        row = next((item for item in artifacts if item["kind"] == kind), None)
        if not row:
            raise NotFoundError("artifact not found")
        return row

    def revise(self, edit_id: str, instruction: str, user_id: str | None = None) -> dict[str, Any]:
        with self._connect() as c:
            edit = c.execute("SELECT * FROM edits WHERE id=%s AND (%s::text IS NULL OR user_id=%s) FOR UPDATE", (edit_id, user_id, user_id)).fetchone()
            if not edit:
                raise NotFoundError("edit not found")
            if edit["state"] not in {"awaiting_approval", "approved", "completed", "failed"}:
                raise ConflictError("edit is not revisable while planning or final rendering")
            parent = c.execute("SELECT iteration FROM plans WHERE edit_id=%s ORDER BY iteration DESC LIMIT 1", (edit_id,)).fetchone()
            if not parent:
                raise ConflictError("revision requires a previous validated plan")
            number = edit["current_iteration"] + 1
            c.execute("INSERT INTO iterations(edit_id,iteration,parent_iteration,instruction,user_id) VALUES (%s,%s,%s,%s,%s)", (edit_id, number, parent["iteration"], instruction, edit["user_id"]))
            self._queue(c, edit_id, number, "revision")
            return c.execute("UPDATE edits SET current_iteration=%s,approved_iteration=NULL,state='planning',failure=NULL,progress=%s,updated_at=now() WHERE id=%s RETURNING *", (number, json.dumps({"stage": "revision", "status": "queued", "iteration": number}), edit_id)).fetchone()

    def get_iterations(self, edit_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            if not c.execute("SELECT 1 FROM edits WHERE id=%s AND (%s::text IS NULL OR user_id=%s)", (edit_id, user_id, user_id)).fetchone():
                raise NotFoundError("edit not found")
            return c.execute("SELECT i.*,p.id AS plan_id FROM iterations i LEFT JOIN plans p USING(edit_id,iteration) WHERE i.edit_id=%s ORDER BY iteration", (edit_id,)).fetchall()

    @staticmethod
    def _queue(c, edit_id: str, iteration: int, kind: str) -> None:
        c.execute("INSERT INTO jobs(id,edit_id,iteration,kind,status,progress) VALUES (%s,%s,%s,%s,'queued',%s)", (new_id("job"), edit_id, iteration, kind, json.dumps({"stage": kind, "status": "queued"})))

    @staticmethod
    def _owned(c, job: ClaimedJob) -> None:
        if not c.execute("SELECT 1 FROM jobs WHERE id=%s AND status='running' AND claimed_by=%s AND attempts=%s AND lease_until>now() FOR UPDATE", (job.id, job.claimed_by, job.attempts)).fetchone():
            raise ConflictError("job lease is no longer owned")

    @staticmethod
    def _finish(c, job: ClaimedJob) -> None:
        c.execute("UPDATE jobs SET status='succeeded',lease_until=NULL,finished_at=now(),progress=%s,updated_at=now() WHERE id=%s", (json.dumps({"stage": job.kind, "status": "succeeded", "iteration": job.iteration}), job.id))
        c.execute("UPDATE job_attempts SET status='succeeded',finished_at=now() WHERE job_id=%s AND attempt=%s", (job.id, job.attempts))

    @staticmethod
    def _progress(c, job: ClaimedJob, status: str) -> None:
        progress = json.dumps({"stage": job.kind, "status": status, "iteration": job.iteration, "attempt": job.attempts})
        c.execute("UPDATE jobs SET progress=%s WHERE id=%s", (progress, job.id))
        if job.kind in {"plan", "revision", "compilation", "render"}:
            state = "rendering" if job.kind == "render" else "planning"
            c.execute("UPDATE edits SET state=%s,progress=%s,updated_at=now() WHERE id=%s AND (current_iteration=%s OR (approved_iteration=%s AND %s='render'))", (state, progress, job.edit_id, job.iteration, job.iteration, job.kind))
        if job.kind in {"preview", "inspection", "render"}:
            column = "render_status" if job.kind == "render" else "preview_status"
            c.execute(f"UPDATE iterations SET {column}=%s,updated_at=now() WHERE edit_id=%s AND iteration=%s", (status, job.edit_id, job.iteration))
            if job.kind == "render":
                c.execute("UPDATE iterations SET status='rendering' WHERE edit_id=%s AND iteration=%s", (job.edit_id, job.iteration))

    @staticmethod
    def _failure(c, edit_id: str, iteration: int, kind: str, error: dict[str, Any]) -> None:
        payload = json.dumps(error)
        if kind in {"preview", "inspection"}:
            c.execute("UPDATE iterations SET preview_status='failed',failure=%s,updated_at=now() WHERE edit_id=%s AND iteration=%s", (payload, edit_id, iteration))
        else:
            c.execute("UPDATE iterations SET status='failed',failure=%s,render_status=CASE WHEN %s='render' THEN 'failed' ELSE render_status END,updated_at=now() WHERE edit_id=%s AND iteration=%s", (payload, kind, edit_id, iteration))
            c.execute("UPDATE edits SET state='failed',failure=%s,progress=%s,updated_at=now() WHERE id=%s AND (current_iteration=%s OR (approved_iteration=%s AND %s='render'))", (payload, json.dumps({"stage": kind, "status": "failed"}), edit_id, iteration, iteration, kind))

    def complete_compilation(self, job: ClaimedJob) -> None:
        with self._connect() as c:
            self._owned(c, job)
            c.execute("UPDATE iterations SET status='awaiting_approval',artifact_paths=artifact_paths || jsonb_build_object('mlt',(artifact_paths->>'workspace') || '/project.mlt'),updated_at=now() WHERE edit_id=%s AND iteration=%s", (job.edit_id, job.iteration))
            c.execute("UPDATE edits SET state='awaiting_approval',progress=%s,updated_at=now() WHERE id=%s AND current_iteration=%s", (json.dumps({"stage": "awaiting_approval", "iteration": job.iteration}), job.edit_id, job.iteration))
            self._finish(c, job)

    def complete_preview_render(self, job: ClaimedJob) -> None:
        with self._connect() as c:
            self._owned(c, job)
            self._finish(c, job)
            self._queue(c, job.edit_id, job.iteration, "inspection")
