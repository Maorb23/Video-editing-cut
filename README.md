# Shotcut-Compatible Video Editing Skill

This repository contains a local, deterministic Python editing engine and a
Codex plugin that turns natural-language requests into versioned JSON edit
plans. The Python core validates plans, generates editable Shotcut/MLT
projects, renders MP4 through `melt`, and creates immutable inspection passes.
Codex is not required to run any core command.

Phase 0 also includes a standalone runner. It creates a fresh confined job,
copies the source without modifying it, generates a CFR analysis proxy and a
bounded set of evidence frames, asks a configured structured-output model for
an edit-plan 1.0 draft, validates and compiles it, renders a unique attempt,
fully decodes and probes the output, and publishes `result.json` only after all
gates pass.

## Status

The repository is an implementation-oriented V1. It has typed, strict plan
validation and explicit mappings for cuts, tracks, captions, overlays,
transitions, transforms/keyframes, audio controls, speed, chroma key, masks,
and a curated filter registry. Actual render compatibility depends on the
installed Shotcut/MLT build; see the pinned compatibility matrix. Automated
cross-platform and Shotcut open/save certification is not claimed until its
matrix rows are populated with evidence.

## Job contract

```text
job/
├── plan.md
├── media-manifest.json
├── edit-plan.json
├── project.mlt
├── final.mp4
├── assets/
└── review/
    ├── metadata.json
    └── passes/pass-001/
```

Source media and user-provided MLT projects are read-only inputs. Output paths
that already exist are refused unless the artifact is tool-owned and the
specific command documents safe replacement behavior.

## CLI

Every plugin script is directly executable with Python and exposes
`main(argv: list[str] | None = None) -> int`:

```bash
python plugins/video-editing-skill/skills/video-editing-skill/scripts/check_environment.py --json
python plugins/video-editing-skill/skills/video-editing-skill/scripts/probe_media.py clip.mp4 --output media-manifest.json
python plugins/video-editing-skill/skills/video-editing-skill/scripts/validate_edit_plan.py edit-plan.json
python plugins/video-editing-skill/skills/video-editing-skill/scripts/compile_project.py edit-plan.json --output project.mlt
python plugins/video-editing-skill/skills/video-editing-skill/scripts/render_project.py project.mlt --output final.mp4 --quality preview
python plugins/video-editing-skill/skills/video-editing-skill/scripts/inspect_video.py final.mp4 --edit-plan edit-plan.json --output-dir review
python plugins/video-editing-skill/skills/video-editing-skill/scripts/video_viewer.py final.mp4 --project project.mlt --plan plan.md
```

The standalone installed command uses the OpenAI Responses provider by
default and requires `OPENAI_API_KEY`; the analyzer and model are injectable
Python protocols for other providers and deterministic tests:

```bash
video-edit-run source.mp4 "Remove the first second and add a title" \
  --output-dir jobs/edit-001 \
  --melt /absolute/path/to/melt \
  --ffmpeg /absolute/path/to/ffmpeg \
  --ffprobe /absolute/path/to/ffprobe
```

The output directory must be new or empty. Successful jobs contain
`edit-plan.json`, `project.mlt`, `analysis/`, a unique
`renders/render-*/output.mp4`, its validation record, and `result.json` with
the sole accepted-render pointer. Failures create `failure.json` and never
publish an accepted result.

No command installs dependencies. Use `check_environment.py` to discover
Shotcut's bundled `melt`, standalone MLT, FFmpeg, and FFprobe.

## Phase 1 service

Install the explicit service extra in the deployment environment, create a
PostgreSQL database, and select filesystem or S3-compatible object storage.
The API and one worker are separate processes over the same database; no
Redis or distributed queue is used.

```bash
pip install -e '.[service]'
# Include database credentials when PostgreSQL requires a password. URL-encode
# reserved characters in the password (for example, @ becomes %40).
export VIDEO_EDIT_DATABASE_URL='postgresql://postgres:YOUR_PASSWORD@127.0.0.1:5432/video_editing'
export VIDEO_EDIT_STORAGE=filesystem
export VIDEO_EDIT_STORAGE_ROOT="$PWD/service-data/objects"
export VIDEO_EDIT_WORK_ROOT="$PWD/service-data/jobs"

# Resend 
export MELVID_EMAIL_PROVIDER=resend
export MELVID_EMAIL_ENDPOINT=https://api.resend.com/emails
export MELVID_EMAIL_API_KEY='re_your_actual_key'
export MELVID_EMAIL_FROM='Melvid <no-reply@melvid.app>'
export MELVID_PUBLIC_BASE_URL='http://127.0.0.1:8000'
# Optional when these programs are on PATH; required when they are elsewhere.
export VIDEO_EDIT_FFMPEG="$(command -v ffmpeg)"
export VIDEO_EDIT_FFPROBE="$(command -v ffprobe)"
export VIDEO_EDIT_MELT="$(command -v melt)"
# Defaults shown explicitly for predictable local worker behavior.
export VIDEO_EDIT_MAX_UPLOAD_BYTES=$((2 * 1024 * 1024 * 1024))
export VIDEO_EDIT_WORKER_LEASE_SECONDS=300
export VIDEO_EDIT_WORKER_MAX_ATTEMPTS=3
# Local HTTP keeps this false. Set it to true in HTTPS deployments.
export VIDEO_EDIT_SESSION_COOKIE_SECURE=false
# Required in deployed environments; keep it stable so sessions remain valid.
export VIDEO_EDIT_DJANGO_SECRET_KEY='replace-with-a-long-random-secret'
mkdir -p "$VIDEO_EDIT_STORAGE_ROOT" "$VIDEO_EDIT_WORK_ROOT"
video-edit-migrate
video-edit-api
# In a second process:
video-edit-worker
```

`video-edit-migrate` applies both the service SQL migrations and Django's
account/session migrations. Authentication uses Django's user model, password
hashing, and database session store while preserving the `/v1/auth/*` API.

For the local Docker PostgreSQL example below, use
`postgresql://postgres:postgres@127.0.0.1:5432/video_editing`. If migration
reports `fe_sendauth: no password supplied`, the database URL is missing its
password. Do not commit real credentials; load them from your shell, a secret
manager, or your deployment environment.

For S3-compatible storage, install `.[service,s3]` and set
`VIDEO_EDIT_STORAGE=s3`, `VIDEO_EDIT_S3_BUCKET`, and optionally the endpoint
and region variables. The REST workflow is upload, create edit, inspect the
proposed edit-plan 1.0, approve it, explicitly request rendering, poll status,
then fetch the uniquely accepted validated result.

### Local Docker worker: two-terminal workflow

The following development workflow uses PostgreSQL and the worker in Docker,
while the API runs from the host virtual environment. Run every command from
the repository root. Create the local PostgreSQL container once if it does not
already exist (the credentials below are for local testing only):

```bash
docker run -d \
  --name postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=video_editing \
  -p 5432:5432 \
  -v postgres_data:/var/lib/postgresql/data \
  postgres:16

docker build -f Containerfile.worker -t video-edit-worker:phase3 .
```

In terminal 1, start PostgreSQL, configure the host API, apply migrations, and
run the API:

```bash
docker start postgres
export VIDEO_EDIT_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/video_editing'
export VIDEO_EDIT_STORAGE=filesystem
export VIDEO_EDIT_STORAGE_ROOT="$PWD/service-data/objects"
export VIDEO_EDIT_WORK_ROOT="$PWD/service-data/jobs"
export VIDEO_EDIT_MAX_UPLOAD_BYTES=$((2 * 1024 * 1024 * 1024))
export VIDEO_EDIT_WORKER_LEASE_SECONDS=300
export VIDEO_EDIT_WORKER_MAX_ATTEMPTS=3
mkdir -p "$VIDEO_EDIT_STORAGE_ROOT" "$VIDEO_EDIT_WORK_ROOT"
video-edit-migrate
video-edit-api
```

In terminal 2, load the API key without putting it in shell history and run the
worker in the foreground. `host.docker.internal` lets the worker reach the
PostgreSQL port published by the host. The bind mounts make API and worker use
the same files.

```bash
read -rsp "OpenAI API key: " OPENAI_API_KEY
echo
export OPENAI_API_KEY

docker run --rm \
  --name video-edit-worker-test \
  --user "$(id -u):$(id -g)" \
  -e OPENAI_API_KEY \
  -e VIDEO_EDIT_DATABASE_URL='postgresql://postgres:postgres@host.docker.internal:5432/video_editing' \
  -e VIDEO_EDIT_STORAGE=filesystem \
  -e VIDEO_EDIT_STORAGE_ROOT=/var/lib/video-editing/objects \
  -e VIDEO_EDIT_WORK_ROOT=/var/lib/video-editing/jobs \
  -e VIDEO_EDIT_MAX_UPLOAD_BYTES=$((2 * 1024 * 1024 * 1024)) \
  -e VIDEO_EDIT_WORKER_LEASE_SECONDS=300 \
  -e VIDEO_EDIT_WORKER_MAX_ATTEMPTS=3 \
  -e VIDEO_EDIT_MODEL=gpt-5.6-luna \
  -v "$PWD/service-data/objects:/var/lib/video-editing/objects" \
  -v "$PWD/service-data/jobs:/var/lib/video-editing/jobs" \
  video-edit-worker:phase3
```

Open `http://127.0.0.1:8000/`, upload a video, create an edit, approve the
proposed plan, and request rendering. Terminal 2 displays the structured worker
events. Stop either foreground process with `Ctrl+C`; the PostgreSQL data
remains in the `postgres_data` Docker volume.

## Phase 2 web application

When the Phase 1 API is running, open `http://127.0.0.1:8000/` for the
minimal product workflow. The page uses only the documented `/v1` endpoints:
it uploads a video, submits an instruction, and shows the compiled plan before
any video is rendered. Users can review public observations, decisions,
confidence, assumptions and unsupported requests, select previous iterations,
request changes, or approve a selected iteration for rendering and inspection.
Refreshing the browser recovers the edit. Prior final exports remain downloadable.

The page also records funnel events and structured outcome feedback locally in
the browser. Use **Export feedback** to provide the JSON record to the team;
the iteration API extends the existing REST and storage boundary.

### Browser tests

Phase 2 interaction tests use Playwright's matching Chromium build. Install it
once after installing the pinned browser-test dependency. To use another
Chromium-family browser, set `VIDEO_EDIT_BROWSER_EXECUTABLE` explicitly.
An existing isolated browser debugging session can instead be selected with
`VIDEO_EDIT_BROWSER_CDP` (a CDP websocket endpoint).

```bash
pip install -e '.[service,browser-tests]'
python -m playwright install chromium
python -m pytest tests/browser/test_web_browser.py -q
```

## Phase 3 reliability baseline

The first Phase 3 slice addresses failures already observed in the local and
service workflows. PostgreSQL now records immutable worker attempts, retries
only classified transient failures, reclaims expired leases, and permanently
fails a stale job at the configured attempt ceiling. Worker lifecycle records
are emitted as structured JSON logs with job/edit IDs, stage, attempt, elapsed
time, and stable failure code.

The current trusted single-worker tier intentionally retains PostgreSQL queue
claiming and concurrency one. Redis, autoscaling, MCP, billing, and organization-
level tenancy remain deferred until workload or user evidence justifies them.
This tier's configured safeguards are:

- three worker attempts by default (`VIDEO_EDIT_WORKER_MAX_ATTEMPTS`, bounded
  from 1 to 10);
- 300-second renewable leases;
- the existing 600-second process deadline, 120-second no-progress deadline,
  256 KiB bounded diagnostics, process-group cleanup, and cancellation token;
- immutable source/object writes and job-confined workspaces;
- a non-root worker image in `Containerfile.worker`.

Deploy the worker image with an orchestrator-enforced read-only root filesystem,
CPU/RAM/PID limits, quota-limited writable job/object mounts, and a network
policy permitting only PostgreSQL and the configured object store. Container
resource limits are deployment controls and must not be inferred from the
image alone.

## SaaS account deployment

The web application includes Django-backed accounts and sessions, email
verification/password-reset links, user-scoped project history, avatars, and
an immutable credit ledger. Run `video-edit-migrate` before deploying the API.
The top-up endpoint is deliberately mocked and never accepts card data. Prices
come from the backend; a future payment provider must confirm an order on the
server before adding an idempotent ledger event.

Railway should retain the existing API, worker, and PostgreSQL services. Add a
Redis service for shared signup/login rate limits in production. Configure
Cloudflare Turnstile and a transactional email HTTP relay for public signup;
without those variables local development uses an in-process limiter, skips
CAPTCHA enforcement, and suppresses delivery. See `.env.example`. Production
HTTPS deployments must set `VIDEO_EDIT_SESSION_COOKIE_SECURE=true` and a
stable random `VIDEO_EDIT_DJANGO_SECRET_KEY`.

For Resend, configure `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, and
`DEFAULT_FROM_MAIL=Readwoods <verify@readwoods.com>` using a domain verified in
Resend. The API endpoint defaults to `https://api.resend.com/emails`.
The older `MELVID_EMAIL_*` names remain accepted as migration aliases.

For Railway, set `VIDEO_EDIT_DATABASE_URL` to the Postgres service reference
`${{Postgres.DATABASE_URL}}`, or set the standard `DATABASE_URL` variable. Do
not use `127.0.0.1` or `localhost`: those addresses point inside the API
container, not to Railway's Postgres service.

The API service can be deployed directly from source with the repository
`Procfile`; Railway supplies `$PORT` and the command binds to `0.0.0.0`. Keep
the worker as its own Railway service using `Containerfile.worker` (or an
equivalent worker start command), sharing the same database, object storage,
and required environment variables.

Set `OPENAI_API_KEY` on the worker service only and seal it in Railway. The API
service does not need the key. Use a dedicated OpenAI project/service-account
key, apply project spend and rate limits, and rotate the Railway variable if
the key is ever exposed. Staff (`is_staff=true`) accounts are an explicit
unmetered operator tier: edits created while the account is staff snapshot
`billing_exempt=true` and do not consume application credits. OpenAI and
infrastructure usage still accrue to the operator account.

`railway.toml` pins the API build command to install the optional service,
Redis, and S3 dependencies and runs `video-edit-migrate` as a pre-deploy
command. If a
Railway service has explicit dashboard overrides, set the build command to
`pip install -e '.[service,redis]'` and the pre-deploy command to
`video-edit-migrate` (or remove the overrides so the checked-in configuration
is used); otherwise `uvicorn` may be unavailable or the SaaS tables may not
exist at runtime.

Run the deterministic suite with `python -m unittest discover -v`. The pinned
render lane is intentionally explicit so CI cannot silently substitute tools:

```bash
VIDEO_EDIT_REQUIRE_PINNED_TOOLCHAIN=1 \
VIDEO_EDIT_PINNED_MELT=/absolute/path/to/melt \
VIDEO_EDIT_PINNED_MELT_VERSION=7.40.0 \
python -m unittest tests.integration.test_pinned_toolchain -v
```

## Iterative review workflow

Apply migrations with `video-edit-migrate` before starting the updated API and
worker. Planning and rendering remain asynchronous. Each edit starts at
iteration 1; the UI formats that as `001`.

- `POST /v1/edits/{edit_id}/revise` accepts `{"instruction":"Reduce the zoom"}`
  and returns HTTP 202 with the new iteration and its `plan_url`. Poll the edit
  until planning and compilation finish, then fetch that URL for the complete
  validated plan and its persisted `decision_log`.
- `GET /v1/edits/{edit_id}/iterations` lists history, parent references,
  instructions, timestamps, artifact locations and errors.
- `GET /v1/edits/{edit_id}/iterations/{iteration}/plan` returns a specific
  version, including separate plan, preview and final-render statuses.
- Legacy `/preview`, `/poster` and `/inspection` artifacts remain readable for
  existing iterations. New iterations do not render video before approval.
  Video downloads support byte ranges. A completed render is available at `/video`.
- Approval still accepts a `plan_id`; it binds that exact iteration. The UI's
  **Approve and render** action calls `/approve` followed by `/render`.

Worker jobs retain support for `plan`, `revision`, `compilation`, `preview`,
`inspection` and `render`, with queued/running/succeeded/failed lifecycle records
in the edit response. New work queues rendering only after approval. Revision is
allowed after planning and outside final rendering; it uses the original
uploaded source and the latest validated plan as context. Unsupported requests
are listed explicitly, and approval applies only the supported operations.

Artifacts live beneath `jobs/{edit_id}/iterations/001/`, including
`edit-plan.json`, `decisions.json`, `project.mlt`, `preview.mp4`, `poster.png`
and `inspection.json`. Render attempts and extracted evidence have their own
subdirectories. Retried planning uses `attempts/002/` inside its iteration so
failed attempts are retained. Database content records and object writes are
immutable; only lifecycle fields and `latest.json` pointers advance.
`current_iteration` identifies the latest request, `active_iteration` the
latest successful preview/result (or explicitly selected final result), and
`approved_iteration` the version selected for final rendering. A prior accepted
export stays downloadable while a new iteration is being prepared.

Source frame rates are preserved as rational values. Video tracks retain
embedded audio unless muted, and audio-only tracks hide video. Avoid duplicating
the source on two audible tracks unless mixing is intended. Fractional
brightness `0.35` compiles to MLT `level=1.35`; negative fractional amounts
darken. Legacy native brightness levels above 1 retain their existing meaning.
Transform rectangles describe output size: 150% enlarges, while 60% shrinks.
Audio fades convert linear amplitude endpoints into MLT decibels.

The essential integration checks are `tests/integration/test_iterations_postgres.py`
for transactions/API behavior and `tests/integration/container_iteration_e2e.py`
for real three-iteration rendering in `video-edit-worker:phase3`. The latter
requires an isolated `VIDEO_EDIT_TEST_DATABASE_URL`, runs the real HTTP API and
worker with FFmpeg/FFprobe/Melt, and injects only deterministic model responses.
It checks brightness, reduced zoom, audible audio, fade amplitude, rational
frame rate, unsupported pitch shifting, downloads and immutable prior results.
It does not test live-model subject recognition. `VIDEO_EDIT_E2E_ROOT` selects
where its retained jobs and `verification.json` are written. Run it under the
worker's Xvfb child-process setup, as in `service/worker-entrypoint.sh`.

## Plan shape

See `references/edit-plan-v1.md` and `examples/minimal/edit-plan.json`. All
times in executable plan fields are integer frames. Helpers parse rational
seconds and convert only after the profile is fixed, preserving rates such as
30000/1001 exactly.

## Existing MLT projects

`--base-project` is conservative. The compiler parses and clones the source
tree, never writes it, preserves unknown XML nodes/properties semantically,
and appends a namespaced generated tractor. It refuses ID collisions,
unsupported root structures, and operations that claim to target nodes not
owned by this tool. XML formatting and attribute order may change when a base
project is serialized; byte identity is therefore not promised.
