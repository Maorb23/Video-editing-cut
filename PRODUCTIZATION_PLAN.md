# Productization Plan

Audit date: 2026-09-03

This plan is based on the current repository at commit `c61b36e`, the checked-out generated artifacts, and a read-only execution of the environment check, plan validator, FFprobe, and test suite. The working tree already contained unrelated deleted and untracked demo/review artifacts before this document was added; those artifacts were inspected but not changed.

## 1. Executive summary

The repository already has the most important architectural seam for productization:

```text
natural-language reasoning (Codex today)
        -> versioned edit-plan JSON
        -> strict Python validation
        -> deterministic timeline resolution and MLT compilation
        -> melt rendering
        -> FFmpeg/FFprobe inspection evidence
```

The model does **not** generate MLT directly. `plugins/video-editing-skill/skills/video-editing-skill/SKILL.md` tells Codex to author `plan.md` and `edit-plan.json`; `video_editing.plan`, `video_editing.operations`, and `video_editing.mlt` then validate and compile that plan without Codex. This is the correct foundation and should be extracted incrementally, not rewritten.

However, the current repository is a deterministic editing core plus a human/agent operating procedure, not yet a standalone agentic editing application. There is no backend component that accepts natural language, calls a model, extracts semantic source-video evidence, creates a plan, tracks approval, or drives retries. Codex itself currently provides all of that orchestration and judgment. The optional transcript and visual-analysis adapters in `video_editing/adapters.py` are only shallow validators and are not connected to planning or compilation.

The largest architectural risk is therefore hidden orchestration: essential behavior lives in the Codex conversation and tool loop rather than in versioned service code or persisted job state. The generated demos prove the loop can work, but also show why it must be formalized. One demo first rendered blank video and almost-silent audio because `melt` could not open the source path; another inspection used CFR timeline frame 642 against a VFR source with only 640 decoded frames. Codex diagnosed and corrected both. There is no durable attempt model, model/prompt provenance, immutable plan snapshot per render, or reliable designation of the accepted output; in fact, some `final.mp4` files in the demo tree are failed or corrupt while a later `final-pass-NNN.mp4` is the accepted render.

The smallest useful product system is one API service, PostgreSQL, filesystem or S3-compatible artifact storage, and one Python background worker. The worker initially runs analysis, planning, validation, compilation, rendering, and inspection sequentially. A dedicated queue, multiple worker classes, containerized render fleet, and sophisticated scheduling should be added only after real workload data justifies them.

The current edit plan is already the intermediate representation (IR). Keep it for the standalone extraction. Do not make an IR redesign a prerequisite for shipping the first API. Close only correctness and safety gaps that block the standalone flow, and place job/model/render provenance in separate manifests or database records.

## 2. Current architecture

### 2.1 Repository map and entry points

| Area | Actual files | Responsibility |
| --- | --- | --- |
| Codex plugin metadata | `plugins/video-editing-skill/.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `agents/openai.yaml` | Makes the skill discoverable and supplies display/default prompt metadata. |
| Agent operating procedure | `plugins/video-editing-skill/skills/video-editing-skill/SKILL.md` | Defines Plan -> Preflight -> Analyze -> Compile -> Render -> Inspect -> Iterate, confirmation, artifact names, and review rules. This is effectively the current application orchestrator. |
| Skill references | `references/edit-plan-v1.md`, `analysis-adapters.md`, `mlt-mappings.md`, `existing-mlt.md`, `compatibility.md` | Describes the edit-plan contract, optional semantic artifacts, allow-listed MLT mappings, conservative base-project behavior, and an unverified compatibility target. |
| Plugin script shims | `plugins/.../scripts/*.py` | Import `_bootstrap.py`, add the repository root to `sys.path`, and delegate to the package CLIs. |
| Installed CLI entry points | `[project.scripts]` in `pyproject.toml`, `video_editing/cli/*.py` | `video-edit-check`, `video-edit-probe`, `video-edit-validate`, `video-edit-compile`, `video-edit-render`, `video-edit-inspect`, and `video-edit-viewer`. |
| Deterministic plan core | `video_editing/plan.py`, `operations.py`, `timebase.py`, `filters.py`, `errors.py` | Strict property allow-listing, semantic checks, rational frame conversion, structural timeline mutation, filter registry, and structured domain errors. |
| Media/tool integration | `environment.py`, `probe.py`, `process.py` | Finds local executables, invokes FFprobe, hashes source files, and provides list-form subprocess execution. |
| Compiler/renderer | `mlt.py`, `render.py` | Compiles an edit plan to MLT XML and invokes `melt` to an atomic partial MP4. |
| Inspection/review | `inspect.py`, `viewer.py`, `assets/viewer.html` | Extracts regular/boundary/keyframe frames, gathers audio diagnostics, performs limited transform SSIM checks, and serves a local review page. |
| Tests/examples | `tests/*.py`, `examples/minimal/*`, `demo/*` | 37 discovered tests, one skipped here because `melt` is absent; sample plans plus real generated attempts and review evidence. |

There are no JavaScript/TypeScript modules, model SDKs, web frameworks, database clients, queue clients, or runtime Python dependencies in `pyproject.toml`. The only browser asset is a static HTML viewer. External runtime dependencies are locally installed `melt`, FFmpeg, and FFprobe.

### 2.2 Architecture diagram

```text
User + source media
        |
        v
Codex harness loads plugin/SKILL.md
  - interprets natural language
  - invokes shell commands
  - views source/render frames
  - writes plan.md and edit-plan.json
  - manually completes review records
        |
        +---- check_environment.py ---> environment.py ---> local PATH/Shotcut
        +---- probe_media.py ---------> probe.py ----------> ffprobe + SHA-256
        |
        v
edit-plan.json (existing hybrid IR)
        |
        +---- validate_edit_plan.py --> plan.validate_plan()
        |                                 |
        |                                 +--> operations.resolve_timeline()
        v
compile_project.py -------------------> mlt.write_mlt()
        |                                 |
        |                                 +--> ElementTree project.mlt
        v
render_project.py --------------------> render.render() ---> melt ---> partial MP4
                                                                    -> final MP4
        |
        v
inspect_video.py ---------------------> inspect.inspect()
        |                                 |
        |                                 +--> ffprobe metadata
        |                                 +--> ffmpeg PNG samples / SSIM
        |                                 +--> audio logs / waveform
        v
Codex visually reviews evidence, edits inspection JSON, and may revise plan/MLT
```

### 2.3 Entry-point behavior

- `video_editing.cli.check_environment.main()` discovers tools. It is the only normal command with explicit external-tool version timeouts (eight seconds per version flag).
- `video_editing.cli.probe_media.main()` calls `create_manifest()` and refuses an existing output. `probe_one()` FFprobes the file and reads the entire file again for SHA-256.
- `video_editing.cli.validate_edit_plan.main()` calls `load_plan()` with optional filesystem checks.
- `video_editing.cli.compile_project.main()` loads/validates and calls `write_mlt()`. It can append generated nodes to a conservatively parsed `--base-project` but never overwrites it.
- `video_editing.cli.render_project.main()` calls `render()` with `preview` or `final` presets.
- `video_editing.cli.inspect_video.main()` either creates an evidence pass or checks a manually completed inspection record.
- `video_editing.cli.video_viewer.main()` copies the current video/project/plan to `review/viewer` and starts `http.server.ThreadingHTTPServer` on localhost by default.
- `video_editing.cli.common.execute()` converts `PlanValidationError`, `VideoEditingError`, and `KeyboardInterrupt` into exit code 2. Unexpected exceptions still escape with a traceback.

### 2.4 Where reasoning and video understanding happen

LLM reasoning is not implemented in Python. It happens in the Codex agent as it follows `SKILL.md`. The agent creates the prose `plan.md`, decides the project profile, converts understood events into frames, writes `edit-plan.json`, interprets inspection images and raw audio logs, and decides whether/how to iterate.

The repository has three different meanings of “analysis,” which should not be conflated:

1. `probe.py` performs deterministic container/stream metadata extraction and hashing. It does not understand scene content.
2. `adapters.py` defines minimal schemas for externally produced transcript and visual observations. No adapter generates these artifacts, and `plan.validate_plan()` merely allow-lists top-level `transcript` and `analysis` fields without loading or validating their referenced content. None of the inspected demo edit plans contains those fields.
3. `inspect.py` analyzes the **rendered output**, not the source for planning. Its visual semantics still depend on Codex/human review of saved PNGs. Its automated checks only cover whether selected non-identity transform frames differ from corresponding source frames.

There is no checked-in prompt for source-video understanding beyond the procedural prose in `SKILL.md`, no model identifier, no model call log, no transcript generator, no OCR, no scene detector, and no persisted source analysis for the demos. The detailed observations in `demo/*/plan.md` (for example the address-bar typing and lyric column in `demo/video-search-keyframe-zoom/plan.md`) must therefore have come from interactive Codex inspection, but the exact frames/model calls are not reproducible from the repository.

### 2.5 Frame sampling and audio inspection

`inspect._sample_frame_reasons()` samples:

- frame 0 and the last output frame;
- one regular frame every `round(frame_rate)` frames, approximately one per second;
- the frame immediately before, at, and after every clip/effect boundary;
- the frame immediately before, at, and after every transform keyframe.

Each frame is extracted by a separate `_extract_frame()` call:

```text
ffmpeg -v error -i VIDEO -vf select=eq(n\,FRAME) -frames:v 1 -fps_mode vfr DESTINATION
```

This is frame-number accurate for a decoded CFR output, but because every invocation starts at the beginning and decodes until the selected frame, the total work approaches quadratic behavior as videos grow. Full-resolution PNGs also consume substantial disk: the 22-second `Video_search` pass stores 55 output frames plus source comparison frames, around 30 MB for output frames alone.

Audio inspection runs three full FFmpeg passes (`volumedetect`, `silencedetect`, and `ebur128`) and a fourth pass for `showwavespic`. It stores mostly unparsed stderr text. There is no transcription, speaker diarization, music/voice classification, or automated A/V synchronization measurement.

### 2.6 Edit representation and validation

`edit-plan.json` is a versioned hybrid IR, not MLT. Version 1.0 contains:

- a rational, fixed project profile;
- fingerprinted assets with plan-relative paths and declared frame durations;
- declarative tracks and clips with timeline/source frame ranges;
- ordered structural and effect operations;
- export intent;
- optional transcript/analysis references.

Supported operation names in `plan.OP_TYPES` are `trim`, `split`, `remove`, `insert`, `reorder`, `transition`, `caption`, `overlay`, `transform`, `volume`, `fade_audio`, `audio_mix`, `speed`, `chroma_key`, `mask`, and `filter`. `operations.resolve_timeline()` applies the five structural operations in array order. Effects remain operations and are mapped by `mlt.py`.

`plan.validate_plan()` correctly rejects unknown root/object properties, unknown operations, duplicate declared IDs, invalid rational profiles, missing source files, initial clip overruns, same-track clip overlap, unsupported filters/properties, unsupported transform interpolation, several missing targets, and some invalid operation ranges. It reruns overlap checks after structural resolution. Timing utilities in `timebase.py` preserve rational rates such as 30000/1001.

Validation is nevertheless incomplete for production:

- asset fingerprint presence/format and equality to the actual file are not checked;
- absolute paths, `..`, symlinks, and paths outside the job are accepted;
- many geometry/color/opacity/gain/pan/mask/chroma values have no type/range/grammar checks;
- `trim` and `insert` can create source ranges beyond an asset after initial validation;
- generated split IDs can collide, and later operations cannot reliably target inserted/split clips;
- effects targeting a clip later removed by structural operations can be silently unused;
- operation start/duration/keyframes are not consistently constrained to their clip/timeline;
- track/asset media kinds are not checked for compatibility;
- transcript/analysis references are not validated or consumed;
- `export` bitrate/pixel-format/movflags fields are accepted, but `render.render()` ignores them and always uses the selected hard-coded `PRESETS` entry;
- several accepted fields are ignored by compilation: overlay/transition `track_id`, `audio_mix.pan`, `chroma_key.edge`, and `mask.mode`; the `audio_mix.gain_db` mapping also needs MLT compatibility verification;
- multiple conflicting speed/effect operations are not rejected or explicitly composed.

Unknown top-level fields fail, but some **known** fields are therefore silently ineffective, which is equally dangerous for instruction-following reliability.

### 2.7 MLT generation and rendering

`mlt.compile_mlt()` uses `xml.etree.ElementTree`, not string concatenation. It creates:

- an MLT 7.28 root/profile;
- one producer per resolved clip (`avformat-novalidate`, `qimage`, or `timewarp`);
- one playlist per plan track, with blanks for gaps;
- producer filters for transform, volume/fade, chroma key, mask, and curated filters;
- additional playlists/producers for captions and overlays;
- a generated `ves_main` tractor, `qtblend` compositing transitions, audio `mix` transitions, and plan transitions;
- generator and filter-catalog metadata.

Generated IDs are prefixed `ves_`. Producers are reordered before playlists because the MLT XML loader resolves references in document order. `write_mlt()` refuses existing output, writes `.<name>.tmp`, and atomically replaces the destination. Base-project mode checks the root/profile, refuses existing `ves_` IDs and `base:` operation targets, deep-copies the tree, and appends a new generated tractor. It does not edit arbitrary existing timeline structures.

Resource paths are made relative only when the source is beneath the MLT output directory; otherwise `_resource_path()` writes an absolute host path. Generated demo projects consequently contain Windows paths such as `C:\Users\maorb\...`, while the current WSL process would resolve the same inputs under `/mnt/c/...`. This makes MLT artifacts host-specific and caused an observed failed render.

`render.render()` invokes a list-form command equivalent to:

```text
melt PROJECT -progress -consumer avformat:.OUTPUT.partial.mp4 \
  vcodec=libx264 crf=18 preset=medium acodec=aac ab=192k pix_fmt=yuv420p \
  f=mp4 movflags=+faststart
```

Preview uses CRF 28/`veryfast`/128k audio. Progress is parsed from merged stdout/stderr and emitted as JSON lines. A successful nonempty partial file is atomically renamed. On an interrupt, the immediate process is terminated and the partial file removed.

There is no timeout, process-group termination, cancellation token, CPU/RAM/disk limit, diagnostics bound, retry policy, output lock, render attempt ID, MLT allow-list enforcement at render time, or post-render decode/probe gate. `render()` accepts any existing MLT path and a caller-supplied executable path. Those are reasonable local CLI affordances but must not cross an API trust boundary.

The checked-in Python does not use `shell=True`, `os.system()`, or construct FFmpeg/melt commands from natural language; `process.run_checked()` and `render.render()` use list-form arguments. That is a strong reusable safety property. Arbitrary shell capability exists at the Codex harness/orchestration layer, where the agent manually chooses and runs commands, not inside the deterministic engine. A service must replace that harness behavior with fixed task implementations rather than expose it through the API.

### 2.8 Error handling and retries today

The deterministic functions use stable error codes through `VideoEditingError`, `PlanValidationError`, and `ExternalToolError`. CLIs return nonzero status for handled failures. There is no automatic retry in the code. “Retry” means Codex reads diagnostics, changes an artifact/name/path/plan, invokes commands again, and creates a new inspection pass.

Inspection pass allocation scans `review/passes`, selects `max + 1`, and creates that directory. It is not concurrency-safe. The pass directory is called immutable, but `inspect()` initially writes every visual/audio review as `pending`; the agent later edits the same `inspection.json` to complete the record. This is an auditability contradiction. A production implementation should preserve raw evidence and append a separate signed review decision/revision.

## 3. Current end-to-end execution flow

The most complete current example is `demo/video-search-keyframe-zoom`. The exact initial Codex conversation/model calls are absent, so the semantic-analysis portion can only be reconstructed up to the persisted artifacts.

1. **User request and skill selection.** Codex selects the plugin through `plugin.json`/`openai.yaml`, reads `SKILL.md`, and creates a job directory in the user's working tree.
2. **Preflight.** The documented command is `scripts/check_environment.py --json`. In this audit, FFmpeg/FFprobe 6.1.1 were found and `melt` was not available in WSL. The demo was evidently rendered in a different Windows/Shotcut environment; the exact `melt` version is not persisted.
3. **Metadata analysis.** `probe_media.py demo/assets/Video_search.mp4 --output .../media-manifest.json` produced a SHA-256 and FFprobe metadata. The source is 1920x1140 H.264/AAC, about 22.315 seconds, with `r_frame_rate=60/1`, `avg_frame_rate=19200000/667463`, and only 640 encoded video frames.
4. **Semantic source understanding.** Codex identified two address-bar typing moments and a lyric page. No source frames, transcript, analysis JSON, model response, or prompt version for this step are persisted. `plan.md` records the resulting rationale and timeline.
5. **Planning.** Codex chose a 30/1 CFR, 669-frame output and wrote `edit-plan.json` with one clip and one transform containing 13 geometry keyframes.
6. **Validation.** `validate_edit_plan.py` calls `load_plan()` and `resolve_timeline()`. The current plan validates successfully.
7. **Compilation.** `compile_project.py` calls `write_mlt()`. `project.mlt` contains an `affine` filter whose `transition.rect` property is the exact semicolon-separated keyframe sequence.
8. **Rendering.** `render_project.py` invokes `melt`. The checked-in `final-pass-001.mp4` is not a valid MP4 (`moov atom not found`). Later renders are valid 1920x1140 H.264/AAC, 669 frames at 30 fps.
9. **Inspection.** `inspect_video.py` persisted 55 regular/boundary/keyframe output PNGs, source comparison PNGs, four audio-analysis products, and `inspection.json`. Pass 1 failed because the inspector attempted source frame 642 even though the VFR source had only 640 decoded frames. Its manual reviews were still pending.
10. **Iteration.** The plan/keyframe was changed from frame 642 to 639, a later MLT/render was produced, and pass 2 contains 13 complete keyframe evidence records, passing transform SSIM checks, 55 manually passed visual reviews, and a manually passed audio review. `final-pass-003.mp4`/`final.mp4` are valid and match the 669-frame profile.

This path demonstrates the intended loop, but it also exposes missing provenance. Pass 1 points to the mutable current `edit-plan.json`, not an immutable plan snapshot; its stored keyframe 642 no longer matches the current plan's 639. The filenames call the accepted render “pass 3” while the inspection record calls it “pass 2.” No database or manifest connects compile, render, inspection, and approval attempts.

The other demos reinforce this:

- `demo/conan-bibi-keyframes` pass 1 contains blank white frames and effectively silent audio because the renderer could not open the source path; pass 2 corrected the host path. Yet `final.mp4` remains the failed, roughly 68 KB render while `final-pass-002.mp4` is the valid accepted output.
- `demo/conan-bibi-keyframes2` pass 1 found that the planned affine animation was absent; a compiler mapping correction produced a passing later render.
- The tracked-but-currently-deleted LeBron plan used `trim` plus `caption`; its untracked review tree shows five attempts, but old v1.0 inspection records do not have the current structured eligibility/keyframe checks.

These are valuable regression fixtures. They should be converted from informal artifacts into explicit tests before refactoring.

## 4. Reusable versus Codex-specific components

### 4.1 Reusable outside Codex

| Component | Reuse assessment |
| --- | --- |
| `timebase.py` | Reuse essentially unchanged. Exact rational conversion is small, deterministic, and well tested. Add overflow/maximum-duration policy at a higher layer. |
| `errors.py` | Reuse, extending errors with stage, retryability, and structured details/correlation IDs. |
| `filters.py` | Reuse the allow-list pattern; verify every mapping against the pinned production MLT image and add numeric ranges/versioned capabilities. |
| `plan.py` | Reuse the contract and strictness philosophy, but migrate to typed models/JSON Schema and close semantic/path/value gaps. |
| `operations.py` | Reuse directly with edit-plan 1.0. Resolve once per run and persist the resolved timeline as diagnostic provenance, without replacing the approved input IR. |
| `mlt.py` | Reuse the ElementTree compiler, generated namespace, producer ordering, and conservative base-project approach. It needs capability validation, path abstraction, export plumbing, and much broader render certification. |
| `probe.py` | Reuse FFprobe parsing and content fingerprints behind a storage/workspace abstraction, size limits, timeouts, and cached digest/metadata records. |
| `render.py` | Reuse command construction, progress parsing, and partial-to-final atomicity inside a supervised worker. Replace direct unrestricted process management. |
| `inspect.py` | Reuse the concepts of boundary/keyframe evidence and independent output probing. Rewrite extraction for batched decoding and split immutable evidence from review decisions. |
| Package CLIs | Keep as local/debug/compatibility adapters over the same library. They are useful for workers and the Codex skill. |

### 4.2 Codex and harness dependencies, and replacements

| Current dependency | Evidence | Backend replacement |
| --- | --- | --- |
| Skill discovery/default prompt | `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `agents/openai.yaml` | Normal REST authentication/routing. Keep the plugin only as a client adapter. |
| Workflow orchestration | `SKILL.md` procedural commands and gates | Persisted job state machine plus idempotent tasks/events. |
| Natural-language interpretation | Codex authors `plan.md` and `edit-plan.json`; no model code exists | `Planner` provider interface with schema-constrained outputs, prompt/version registry, retries, and stored provenance. |
| Source-video semantic inspection | Not implemented; inferred from detailed demo plans | `Analyzer` pipeline that materializes transcript/scene/visual/OCR observations and targeted frames in object storage. |
| Shell/tool invocation | Codex manually runs Python scripts, and may choose paths/environment | Worker code invokes library functions and a supervised list-form process runner with fixed executables/config. No model-generated commands. |
| Harness image viewing | Codex reviews extracted PNGs | Web review UI and/or a review-model adapter consuming bounded stored evidence. |
| Manual mutation of inspection records | Codex fills `review.status`, observations, and planned changes | Immutable `inspection_evidence` plus separate `review_decision` records with actor/model/provenance and append-only revisions. |
| Conversational confirmation | `SKILL.md` requires approval before first compile/render | Explicit plan revision and approval API with optimistic concurrency and audit records. |
| Working-directory context | Skill creates relative job files wherever Codex is operating | Server-issued job IDs, storage keys, and a per-attempt workspace manager. Never accept host paths from callers/models. |
| Repository-layout bootstrap | `scripts/_bootstrap.py` assumes the core is five parents above the shim | Install `video_editing` as a normal wheel in worker/plugin development environments; plugin calls the API or installed CLI. |
| Local PATH/Shotcut discovery | `environment.py` searches PATH and desktop installation paths | Immutable worker image with configured absolute tool paths and startup health/capability checks. |
| Browser opening/local HTTP server | `viewer.py` copies files and calls `webbrowser.open()` | Authenticated web application with signed artifact URLs; retain local viewer only for development. |
| Iterative judgment/retry | Codex interprets errors and names new pass files | Typed failure policy: deterministic retries for transient failures, planner repair for schema errors, and `needs_attention` for semantic/unsupported cases. |

The Codex skill should remain functional during migration by continuing to call the CLIs at first, then optionally calling the same REST API. It must no longer be the only implementation of orchestration.

## 5. Productization risks and gaps

### 5.1 Observed technical gaps

| Gap | Repository evidence | Production consequence |
| --- | --- | --- |
| Host filesystem is part of the plan | asset `path`; `_resource_path()` emits absolute paths; demos contain Windows paths | Tenant escape/data disclosure, nonportable artifacts, repeat of blank/silent renders. |
| Fingerprints are declarative only | `plan.py` allow-lists but does not verify `fingerprint` | Source can change between planning and rendering without detection. |
| Untrusted MLT can be rendered | `render()` accepts any project path | MLT may reference arbitrary files/services/protocols; native parsers execute outside a tenant sandbox. |
| No process limits | `run_checked()` and `render()` have no timeouts or resource controls | Hung FFmpeg/melt, decompression bombs, disk/RAM/CPU exhaustion. |
| Render diagnostics can grow unbounded | `render()` appends every output line | Worker memory exhaustion on noisy/long renders. |
| Fixed partial/output names and pass scan | `.final.mp4.partial.mp4`; `_next_pass()` uses max+1 | Races and cross-attempt corruption under concurrency. |
| Export intent is ignored | `edit-plan.json` accepts bitrates; `render()` uses only `PRESETS` | Valid plans can silently produce the wrong requested encoding. |
| Known properties are ignored | `track_id`, pan, edge, mode and others | Successful validation does not imply instruction execution. |
| No MLT compatibility certification | `compatibility.md` rows are all pending | Accepted plan may compile but fail or render differently on deployed MLT. |
| No MLT preflight | compile creates XML only despite environment text saying “compile validation” | Malformed/unsupported graphs are detected late during an expensive render. |
| VFR timeline/source mapping is ad hoc | `Video_search` has 640 source frames but a 669-frame CFR plan; inspection failed at 642 | Incorrect edit points/evidence and nonreproducible model decisions. |
| Inspection scales poorly | one full-start FFmpeg decode per sampled frame and four audio passes | Long video inspection becomes far slower than real time and consumes excess disk. |
| Inspection is narrow | only transform difference via SSIM; raw audio logs; manual frame review | Cuts, captions, fades, transitions, sync, frozen/black output, and intended content lack robust automated conformance. |
| “Immutable” records are edited | pending review fields are later changed in place | Lost audit history and uncertain accepted evidence. |
| No accepted-output pointer | demos retain failed/corrupt `final.mp4` alongside passing attempts | API could return the wrong artifact. |
| No job or attempt model | filenames are the only linkage | No idempotency, state, cancellation, retry, authorization, or recovery. |
| Semantic pipeline is missing | no model client/source analysis implementation | Standalone service cannot perform the product's main promise. |
| Prompt injection boundary is absent | transcript/visual content would be placed into Codex context | Text shown/spoken in media could attempt to redirect an agent with shell/file access. |
| Error boundary is partial | CLI catches domain errors only | unexpected exceptions crash workers without normalized failure records. |

### 5.2 Must fix before an internal API

An internal API still processes untrusted or accidental inputs and can damage shared infrastructure. Before even internal multi-user use:

1. Introduce server-issued media/job/attempt IDs and a workspace abstraction that confines every local path beneath one job root. Reject absolute paths, traversal, symlink escapes, arbitrary MLT, caller-selected binary paths, and external URL resources.
2. Store uploads in object storage or a quarantined ingest area; stream uploads with hard byte limits, content sniffing, SHA-256, and FFprobe/decode preflight. Reject corrupt media, missing required streams, unsupported codecs/profiles, extreme dimensions/frame rates/durations, and suspicious metadata before planning.
3. Pin exact FFmpeg/FFprobe/MLT versions and configure fixed binary paths instead of relying on desktop Shotcut discovery. For a trusted internal API, the Phase 0 process supervisor and job-local workspace are the minimum boundary; container hardening is required before accepting untrusted/public uploads.
4. Replace `run_checked()`/direct `Popen` with a supervisor supporting wall-clock and no-progress timeouts, bounded logs, process-group termination, cancellation, heartbeats, exit diagnostics, and cleanup. Ensure a killed job cannot publish a partial output.
5. Make the existing IR truly strict: validate every accepted value, ensure every accepted property has a tested compiler effect, verify asset digests, resolve source/timeline/VFR semantics, normalize structural edits, reject incompatible/conflicting operations, and wire export settings through.
6. Add authoritative database job state, immutable plan approval, and a single accepted-artifact pointer. Add full attempts, idempotency, leases, and heartbeats later when concurrency/retry data requires them.
7. Implement the missing analysis/planner interfaces as normal backend code. Persist inputs, bounded model context, model/prompt/schema versions, structured output, cost, and repair attempts. The model must output only the IR—never paths, commands, MLT services, or tool arguments.
8. Treat media/transcript/OCR as untrusted content. Isolate model planning from tool execution, quote/tag evidence as data, use schema-constrained output, prevent it from changing system policy, and run deterministic authorization/capability checks after every model response.
9. Add compile and output gates: XML parse/reference/allow-list checks, short MLT smoke render, FFprobe/decode validation, duration/stream/profile checks, empty/black/frozen checks, and operation-specific assertions. A `melt` zero exit code is not success by itself.
10. Add per-job CPU/RAM/disk estimates and caps, concurrency limits, storage cleanup/retention, structured logs, metrics, traces, and alerts. At minimum track stage latency, tool exit codes, retries, disk high-water marks, model usage, and accepted/rejected outputs.

### 5.3 Must fix before a private beta

- User/org authentication, tenant ownership checks on every row/object, least-privilege service identities, encrypted storage, short-lived signed downloads, and audit logging.
- Containerized non-root render workers with no outbound network, read-only inputs/root filesystem, bounded scratch storage, cgroups/ulimits, and process-count limits before accepting untrusted/public uploads.
- Per-user/org upload, concurrent-job, model-token, render-minute, CPU-hour, and storage quotas; API rate limits and abuse controls.
- Usage metering tied to immutable attempts: uploaded/stored bytes, analyzed minutes, model input/output tokens/images, render CPU/GPU seconds, egress, and retention. Billing can remain manual in beta, but meters must be trustworthy.
- Explicit retention/deletion policies, privacy disclosure, customer deletion, backup/restore for metadata, object lifecycle rules, and support access controls.
- Retry classification and dead-letter handling for model timeouts/rate limits, object-store errors, worker loss, FFmpeg/melt crashes, and transient capacity. Semantic invalidity must not be blindly retried.
- Capacity-aware queue admission, per-tenant fairness, worker autoscaling limits, disk-pressure protection, and graceful deploy/drain behavior.
- Model fallback/degradation policy: retry transient provider errors, optionally switch only to a tested compatible model, never silently weaken quality, and surface `planning_failed`/`needs_attention` with actionable detail.
- Product handling for unsupported/ambiguous instructions. The plan should state unsupported requests and assumptions; the service must not render a plausible but wrong substitute.
- Browser plan review/edit UI, plan diffing, approval audit, cancellation, and clear stage/progress/error reporting.
- A compatibility and regression matrix for every feature actually advertised, using the exact production image and real Shotcut open/save smoke tests if editable-project compatibility is promised.
- Security review/fuzzing of media and XML parsing, dependency/container scanning, secret management, incident logging, and prompt-injection evaluations.

### 5.4 Can wait until later

- MCP adapter and third-party agent ecosystem.
- Multi-region active/active, Kubernetes, separate microservices for every stage, or a workflow engine more complex than the initial queue/state machine.
- Automated billing collection, sophisticated pricing tiers, team collaboration, comments, webhooks, and notification channels.
- GPU rendering or hardware-encoder optimization until measurements justify it.
- Premiere/Resolve export, a second render backend, arbitrary Shotcut project editing, and operations beyond the certified catalog.
- Advanced semantic indexing/search across a user's media library.
- Custom model training/fine-tuning before prompt/model/provider baselines exist.

## 6. Proposed production architecture

### 6.1 Smallest sensible deployment

```text
Browser / own web app
        |
        v
FastAPI control plane
  - upload/edit/plan/approval/result endpoints
  - basic state transitions
        |
        +---------- PostgreSQL (jobs, plans, approval, artifact metadata)
        +---------- filesystem in development / S3 in cloud
        |
        v
One background worker
  probe -> analyze -> plan -> validate existing IR
        -> compile MLT -> melt -> inspect -> publish result
```

Recommended initial technologies:

- Python 3.12 (while retaining library compatibility with 3.10+) and FastAPI/Pydantic for the API/schema boundary.
- PostgreSQL from the start for jobs, plan JSON, approvals, progress, failures, and artifact metadata.
- A small storage interface with local filesystem implementation for development and S3-compatible implementation for cloud deployment.
- One background worker process that claims queued rows from PostgreSQL. Avoid Redis/Celery initially; PostgreSQL `FOR UPDATE SKIP LOCKED` or an equally small lease mechanism is sufficient at this scale.
- One repository and deployable application with separate API and worker process commands. Do not split it into microservices.

### 6.2 Synchronous versus asynchronous work

Synchronous API operations should be short and bounded:

- accept a development upload or return a cloud signed-upload URL;
- create an edit job;
- get status, proposed plan, and result;
- approve the proposed plan;
- request a render;
- return a local development download or cloud signed URL.

Asynchronous work should include:

- hashing when the storage provider does not supply a trustworthy digest;
- FFprobe and corruption/decode checks;
- proxy/contact-sheet/scene/audio/transcript generation;
- model analysis/planning and structured repair;
- media-dependent plan validation and canonical normalization;
- MLT compile/smoke render;
- preview/final rendering;
- output inspection and semantic QA.

Compilation is computationally cheap, but it belongs in the background worker because it resolves workspace media paths and must be coupled to the exact approved plan.

### 6.3 Initial worker boundary and later isolation

For the first trusted internal deployment, one worker can run the complete pipeline. It must still use the Phase 0 workspace and process abstractions: per-job directories, fixed binaries, list-form arguments, timeouts, bounded logs, cancellation hooks, and cleanup. Model code receives bounded analysis artifacts and returns only the existing edit-plan JSON; it does not construct commands or MLT.

Before accepting public/untrusted uploads, harden this worker into a containerized media boundary that:

- runs as a non-root user with a read-only image/root filesystem;
- has no outbound network and only narrowly scoped object-store access, preferably via task-scoped credentials or staged inputs;
- mounts one read-only input directory and one quota-limited scratch/output directory per attempt;
- uses fixed binary paths and a fixed allow-listed environment/locale;
- enforces cgroup CPU/RAM, PID, file-size, open-file, and ephemeral-disk limits;
- kills the complete process group on cancellation/timeout;
- uploads only validated artifacts, then destroys the workspace.

Start with concurrency one. The Phase 3 hardening work can split analysis and render queues/workers when real users show where capacity and isolation are needed.

## 7. Proposed editing IR

### 7.1 Recommendation

Do not introduce a simplistic operation-only format like the example in the request. The repository already has a better IR: rational profile + assets + declarative tracks/clips + operations. It provides multi-track topology, stable IDs, source and timeline ranges, and effect targets that a flat list of timestamp operations cannot express cleanly.

Keep `edit-plan` 1.0 as the contract for the standalone-engine and first API phases. A schema redesign would add migration work before product learning and is not required to remove Codex. The service can store the exact current JSON in PostgreSQL and materialize its relative asset paths only inside a controlled job workspace.

Make only minimum compatible improvements during Phase 0:

- validate existing fields more completely, especially source overruns, operation bounds, fingerprints, and accepted-but-ignored properties;
- add an in-memory resolved timeline snapshot for validation, compilation, and diagnostics while preserving the current input document;
- define a deterministic VFR-to-project-frame conversion policy or use a generated CFR analysis proxy;
- keep job, model, approval, render-attempt, and review provenance outside the edit plan in manifests/database records;
- ensure the natural-language planner emits exactly the existing schema and receives structured validation errors for bounded repair.

The API should use media IDs, but the worker—not the caller or model—maps each media ID to the existing plan's job-relative `assets[].path`. This keeps external paths out of the trust boundary without changing the IR.

### 7.2 Current capability truthfulness

The schema should advertise only mappings certified in the production image. Current code contains mappings for every operation named in `OP_TYPES`, but these are not equally complete:

- Strongest current foundations: trim/split/remove/insert/reorder normalization, caption, static/keyframed affine geometry/opacity, volume/fade, and curated filters.
- Present but requiring render-matrix tests and stronger semantics: overlay, cross-track transition, speed/timewarp, chroma key, mask, track audio mix, rotation, and multiple simultaneous tracks.
- Accepted but currently ignored or questionable: operation `track_id` fields, pan, chroma edge, mask mode, requested export bitrate/pixel format/movflags, and portions of audio-mix behavior.

Unsupported properties must fail at plan validation. Do not expose an “arbitrary MLT property/service” escape hatch.

### 7.3 Value of the existing IR

The IR gives independent retry boundaries: model output can be repaired without rendering; a render can be retried without another model call; a new compiler can consume the same approved plan. It enables deterministic validation, plan diffs/approval, fixtures, metering, provenance, and model swapping. Its declarative track/clip timeline also makes future FFmpeg, Premiere XML, FCPXML, or Resolve adapters possible, but portability must be capability-based: Shotcut/MLT-specific constructs that cannot be represented faithfully should be rejected or explicitly tagged backend-specific, never approximated silently.

Hard-to-abstract MLT concepts include arbitrary service graphs, MLT filter property grammars, transition track routing, timewarp/audio behavior, resource-backed masks, Shotcut UI metadata, and conservative extension of an existing foreign MLT tree. Keep these behind compiler capability declarations. Existing MLT import should remain a separate advanced workflow, not part of the first hosted product.

Consider a version 1.1 only after real plans reveal a concrete limitation that cannot be added compatibly. Do not block the standalone engine, API, or UI on that future decision.

## 8. Proposed REST API

Keep the first API close to the product flow. Use `/v1`, opaque IDs, UTC timestamps, and structured errors.

```text
POST /v1/videos                         upload a video
POST /v1/edits                          submit video + instruction
GET  /v1/edits/{edit_id}                basic state/progress
GET  /v1/edits/{edit_id}/plan           proposed existing edit-plan JSON
POST /v1/edits/{edit_id}/approve        approve that plan
POST /v1/edits/{edit_id}/render         queue rendering
GET  /v1/edits/{edit_id}/result         result and artifact URLs
```

For local development, `POST /videos` can accept a bounded multipart body and store it under the storage root. In cloud deployment it can return a signed S3 upload URL. In both cases it returns a `video_id`; callers never submit a server path.

```json
{
  "id": "vid_01J...",
  "state": "uploaded",
  "filename": "interview.mp4"
}
```

Create an edit:

```json
{
  "video_id": "vid_01J...",
  "instruction": "Remove the setup, keep the dunk and follow-through, and add 'King james' without covering the rim."
}
```

```json
{
  "id": "edt_01J...",
  "state": "analyzing",
  "created_at": "2026-09-03T10:12:30Z"
}
```

`GET /plan` returns a concise human summary plus the unchanged `edit-plan` 1.0 document:

```json
{
  "edit_id": "edt_01J...",
  "plan_id": "pln_01J...",
  "status": "proposed",
  "summary": "11.31-second dunk highlight with an upper-right caption",
  "warnings": [],
  "edit_plan": {"version": "1.0", "profile": {}, "assets": [], "tracks": [], "operations": [], "export": {}}
}
```

Approval should be mandatory initially:

```json
{"plan_id": "pln_01J..."}
```

Rendering is a separate explicit action so the UI can show the approved plan before spending render time. The result endpoint returns only a validated accepted output:

```json
{
  "edit_id": "edt_01J...",
  "state": "completed",
  "video_url": "https://storage/.../output.mp4",
  "artifacts": {
    "edit_plan_url": "https://storage/.../edit-plan.json",
    "mlt_url": "https://storage/.../project.mlt",
    "inspection_url": "https://storage/.../inspection.json"
  }
}
```

Add plan editing, revisions/ETags, cancellation, event streams, separate render resources, idempotency keys, and richer artifact APIs when real clients require them. Internally, even the simple API should preserve the approved plan ID/digest and accepted output pointer.

Errors use stable codes and stage detail:

```json
{
  "error": {
    "code": "unsupported_instruction",
    "stage": "planning",
    "message": "The requested face replacement is not supported.",
    "retryable": false,
    "details": {"unsupported": ["face replacement"]},
    "request_id": "req_01J..."
  }
}
```

## 9. Job lifecycle and state machine

Start with one understandable edit state and keep detailed worker-stage diagnostics in separate columns/events.

```text
uploaded
   -> analyzing
   -> planning
   -> awaiting_approval
   -> approved
   -> rendering
   -> completed

Any processing state -> failed
```

The worker can record a more precise `stage` such as `probing`, `extracting_frames`, `calling_model`, `validating_plan`, `compiling_mlt`, `running_melt`, or `inspecting`, without exposing a complicated public state machine.

Minimum rules:

- PostgreSQL is authoritative; the worker claims a job transactionally and records progress/failure.
- Approval names the exact proposed plan ID/digest.
- Rendering cannot start until that plan is approved.
- Only a rendered output that passes the minimum probe/inspection gate can become the result and move the edit to `completed`.
- A failure records its stage, stable error code, message, and whether a manual retry might help.
- `needs_attention` is preferable to a fabricated plan when the request is ambiguous, unsupported, low confidence, or requires a user choice.

Add cancellation, leases/heartbeats, automatic retries, multiple plan revisions, multiple render attempts, and append-only event history in Phase 3 when user behavior and failure data justify the extra states.

## 10. Model and cost strategy

### 10.1 Where models are actually needed

Today one capable Codex model implicitly handles source semantics, instruction interpretation, planning, and semantic output review. Production should separate these roles:

| Stage | Recommended approach |
| --- | --- |
| Probe, hashing, timebase, scene-change metrics, silence/loudness | Deterministic code only. |
| Speech transcription | Speech-to-text model only when speech, captions, quote removal, pacing, or content selection requires it. Cache by media digest/model version. |
| Coarse visual labeling/OCR | Cheap vision/OCR model or deterministic OCR for scene/keyframe thumbnails; skip when instructions are purely timestamp/metadata based. |
| Instruction decomposition | Text model with structured output after capabilities and media facts are known. |
| Subjective multimodal planning | Strong multimodal reasoning for reframing, highlight selection, visual timing, or ambiguous creative requests. Provide contact sheets/transcript and allow targeted evidence requests. |
| IR normalization/validation/MLT | Deterministic code only. Never ask a model to generate or repair MLT. |
| Output conformance | Deterministic checks first; cheap vision for bounded before/at/after/contact-sheet evidence; strong model only for subjective or failed/uncertain cases. |

### 10.2 Coarse-to-fine analysis

1. Probe and derive duration/stream/timebase deterministically.
2. Generate a low-resolution proxy and scene/change candidates in one decode. Create a bounded contact sheet (for example 12-32 frames for a short clip, logarithmically capped for long media), not one full-resolution image per second sent blindly to an expensive model.
3. Run transcription only when audio/speech is relevant. Use transcript segment boundaries and requested keywords to propose temporal regions.
4. Give a cheaper model the user request, metadata, transcript/OCR, and coarse frames to label scenes and request targeted ranges.
5. Extract denser frames only within candidate ranges and use a strong multimodal model where subject identity, composition, action completion, or creative judgment matters.
6. Generate the structured plan with a constrained schema, then validate deterministically. Allow a small bounded repair loop using validation issues, without resending all video evidence.
7. Review a low-resolution preview first. Render final only after plan approval/preview success when product policy allows.

Persist the analysis manifest and cache it by `(media_sha256, analyzer_version, model_version, proxy_profile)`. Persist model request IDs, token/image counts, latency, cost, prompt/schema version, and output digest. Do not cache across tenants unless privacy policy explicitly allows it.

### 10.3 Prompt-injection containment

Text visible or spoken in a video, metadata tags, filenames, transcripts, and OCR are untrusted data. Model system instructions must explicitly delimit them as evidence. More importantly, the planner must have no shell, filesystem, network, credential, or worker-control tools. Its only output is schema-constrained plan data. A deterministic capability/policy validator independently rejects paths, URLs, commands, unsupported services/properties, excessive resources, and scope changes. The orchestrator—not model text—chooses tasks and tool arguments.

## 11. Performance strategy

### 11.1 Current latency centers

- Upload and SHA-256 are linear in source bytes; `probe_one()` currently reads the entire file after FFprobe rather than combining digesting with ingest.
- Semantic analysis latency is unknown because model calls are not implemented or logged.
- Frame inspection is likely the largest avoidable cost: approximately one regular sample per second plus three per boundary/keyframe, each in a new FFmpeg process decoding from frame zero.
- Audio inspection decodes the full track four times.
- MLT XML generation is negligible.
- Final software x264 rendering through `melt` is likely the dominant unavoidable compute cost, especially at 1080p and `medium` preset.

### 11.2 Concrete improvements

- Hash while uploading or trust/verify object-store checksums; cache FFprobe results by immutable digest.
- Extract all requested frames in one decode using a combined `select` expression or a small number of sequential range decodes. Produce JPEG/WebP thumbnails for model/review and full-resolution PNG only for exact conformance checks.
- Generate a proxy once and reuse it for scene detection, OCR, visual planning, and preview. Preserve a mapping to original timestamps/frames.
- Use scene/change detection plus bounded coarse-to-fine sampling. For long static screen recordings, change/OCR events matter more than one frame per second; for fast action, scene/transcript/motion regions should trigger dense local samples.
- Batch contact sheets for model input and parallelize analysis across independent assets/ranges with strict per-job/global limits.
- Combine compatible audio filters into fewer FFmpeg passes and parse metrics into structured numbers.
- Cache analysis, transcript, normalized IR, compiled MLT, and previews by content/plan/toolchain digest. Never reuse a final render unless every input digest and compiler/render setting matches.
- Render a low-resolution/fast preview for semantic review; render final only after approval. Do not repeatedly final-render plan-repair attempts.
- Measure real-time factor, CPU seconds, peak RSS, scratch bytes, output bytes, frames sampled, and model cost by video minute before considering hardware encoding or GPU workers.
- Set admission limits initially (for example maximum upload bytes, duration, resolution, track count, operation count, and output duration) from benchmarks, not guesses; return those limits in the API.

## 12. Reliability and validation strategy

Each boundary must produce a separately testable artifact and explicit gate.

### 12.1 Natural language -> analysis

- Preserve the original request verbatim and a normalized intent/capability classification.
- Schema-validate transcript/visual/OCR artifacts; ensure timestamps are ordered, bounded, and tied to the exact media digest/timebase.
- Record evidence coverage and uncertainty. A planner may request more evidence but cannot invent unseen events.
- Detect requests outside the certified operation catalog and return `unsupported_instruction` or `needs_attention` before rendering.

### 12.2 Analysis -> proposed plan

- Use JSON Schema/Pydantic with `extra=forbid`, discriminated operation unions, bounded string/list sizes, and finite numeric ranges.
- Validate unique IDs, media/track/clip types, source/timeline ranges, nonoverlap rules, transition topology, effect/keyframe bounds, geometry/color grammar, export capabilities, and maximum resource estimates.
- Verify media digest and metadata version, not merely file existence.
- Require every user instruction to map to a plan element, explicit assumption, or explicit unsupported item. Persist a machine-readable instruction-coverage report.
- Resolve the plan through `operations.resolve_timeline()` and validate the resolved result again. The original edit-plan 1.0 document remains the approval target; store its digest and the resolved diagnostic snapshot together.

### 12.3 Plan -> MLT

- Compile only from a validated in-memory model plus server-resolved workspace paths.
- Parse the emitted XML again and verify IDs/references, profile, resource confinement, in/out ordering, and allow-listed services/properties.
- Compare emitted features against a compiler capability manifest for the pinned toolchain.
- Run `melt` on a bounded synthetic/smoke range or query mode where useful, followed by a short decode, before the full render.
- Persist compiler version, filter-catalog version, toolchain image digest, IR digest, MLT digest, and resolved resource manifest.

### 12.4 MLT -> rendered video

- Supervise the process with deadlines, no-progress timeout, heartbeat, bounded logs, cancellation, and process-group/container cleanup.
- Publish from a unique attempt-local partial path only after success gates; never infer acceptance from filename.
- FFprobe the output and verify container, codecs, dimensions, CFR/rate, expected frame/duration tolerance, audio presence/channels/sample rate, timestamps, and nonzero streams.
- Decode first/middle/last frames and a complete low-resolution pass to catch corrupt tails, empty output, frozen output, blank/solid frames, invalid timestamps, and partial files.
- Compare expected versus actual duration after cuts/speed/transitions using rational calculations. Define tolerances for audio encoder padding separately from video frames.
- Measure A/V PTS drift and discontinuities. Existing loudness/silence checks are useful but do not establish sync.

### 12.5 Rendered video -> accepted result

Add operation-specific conformance tests:

- trim/cut/split/reorder: source-frame/time mapping at both sides of boundaries;
- caption/overlay: absent/present/absent around exact boundaries, OCR/template/alpha placement checks;
- transform/keyframes: geometry/subject containment and interpolation samples, plus identity checks—not merely “different from source” SSIM;
- transitions: expected overlap/duration and no black flash;
- volume/fades/mix: envelope/loudness/channel checks;
- speed: duration and A/V behavior;
- chroma/mask/filter: feature-specific reference tests and visual metrics.

The current one-per-second regular samples are evidence, not a proof. Combine deterministic full-stream checks with targeted visual review. Keep raw evidence immutable; append review decisions with `pass`, `fail`, or `needs_attention`, severity, observation, actor/model, and proposed correction.

Failures shown to users must name the stage and consequence. Examples: “upload is corrupt at 00:31.2,” “request uses unsupported face replacement,” “plan references 2 frames past source duration,” “renderer crashed after 64% and no output was published,” or “caption was not detected at its planned start.” Never return a rendered artifact as completed when a required gate failed. Preserve diagnostic artifacts for a bounded support window.

## 13. Security and isolation considerations

- **Path confinement:** API/model documents contain IDs, never filesystem paths. Resolve IDs to paths within a fresh workspace and verify `resolve()` plus symlink policy before every open. Do not mount the host repository, home directory, Docker socket, or shared tenant roots.
- **Uploaded media:** enforce multipart limits, declared/actual size, MIME sniffing, allowed container/codec/resolution/duration, decode probe, filename normalization, checksum, quarantine state, and decompression/resource budgets. Native codec parsing is untrusted even for internal users.
- **MLT:** render only server-compiled MLT from an approved IR. Revalidate every `resource` and service. Disable network/protocol inputs and arbitrary plugin/services. Do not expose base-project import in initial hosted v1.
- **Processes:** fixed absolute binary paths, list arguments, minimal environment, non-root UID, no shell, no inherited secrets, no network, cgroups/seccomp/AppArmor where available, process-group kill, and unique scratch.
- **Tenancy:** tenant IDs on every metadata row/object prefix, authorization on every API and signed URL, scoped worker credentials, encryption in transit/at rest, and audit logs for plan approval/download/deletion.
- **Models:** planning workers get only task-scoped evidence; redact secrets where possible; configure provider retention/privacy; treat all media-derived text as data; never grant planner tool execution.
- **Abuse/cost:** quotas before queueing, estimated-cost display/policy, rate limiting, concurrency/fair scheduling, and hard output-duration/operation limits.
- **Secrets and logs:** secret manager rather than environment dumps; strip signed URLs and sensitive media text from normal logs; retain bounded tool diagnostics with controlled support access.
- **Cleanup:** attempt workspaces are destroyed in `finally` and by a reconciler for crashed workers. Object lifecycle rules remove partials, expired uploads, proxies, and review frames according to policy. User deletion tombstones metadata and asynchronously verifies object removal.

## 14. Storage and job queue strategy

### 14.1 Object layout

Use immutable keys, never mutable `final.mp4` names as truth:

```text
tenants/{tenant_id}/media/{media_id}/original
tenants/{tenant_id}/media/{media_id}/analysis/{analysis_version}/...
tenants/{tenant_id}/edits/{edit_id}/plans/{revision}/plan.json
tenants/{tenant_id}/edits/{edit_id}/renders/{render_id}/project.mlt
tenants/{tenant_id}/edits/{edit_id}/renders/{render_id}/output.mp4
tenants/{tenant_id}/edits/{edit_id}/renders/{render_id}/inspection/...
```

PostgreSQL stores object key, digest, size, type, owner, creation status, job state, plan/approval, and the accepted result pointer. In local development, the byte store is a configured filesystem root. In cloud deployment, use S3-compatible storage and signed uploads/downloads. PostgreSQL remains authoritative for reachability and state in both modes.

Worker scratch is ephemeral and quota-limited. Download inputs into `input/` read-only, compile under `work/`, write unique `output/attempt-id/`, validate, upload, then delete. A scheduled reconciler finds stale leases/workspaces/multipart uploads and cleans them safely.

### 14.2 Queue semantics

Do not add a separate queue initially. One background process can claim the oldest eligible PostgreSQL job using a transaction and `FOR UPDATE SKIP LOCKED`, process it, and update its state. Store only IDs/state in the database; load plans and artifacts through the storage abstraction.

Use unique per-job output paths and ensure one job cannot be claimed twice. This is enough to learn from early users. In Phase 3, add leases/heartbeats, idempotent attempt records, named analysis/preview/final queues, bounded retries, dead-letter handling, and fair per-tenant scheduling when concurrency or worker recovery becomes a demonstrated problem.

## 15. MCP adapter design

MCP should be a thin authenticated client of the same REST API, not a render runtime or source of truth. It can be deployed separately and hold no media-processing binaries.

Useful tools:

- `create_upload` / `complete_upload` (more practical than sending large video bytes through MCP; return a signed upload URL and media ID);
- `get_media_status`;
- `analyze_video` (usually creates/advances an edit rather than exposing internal arbitrary analysis);
- `create_edit_plan`;
- `get_edit_plan`;
- `update_edit_plan` with revision/ETag;
- `approve_edit_plan`;
- `render_video`;
- `get_edit_status` / `get_render_status`;
- `cancel_edit` / `cancel_render`;
- `list_artifacts` / `get_output` returning signed URLs or resources.

Agent-facing capabilities should expose typed plans, warnings, supported operations, approval, status, and artifacts. They should **not** expose arbitrary shell/FFmpeg/melt commands, local paths, binary overrides, raw MLT services/properties, unsigned object keys, cross-tenant search, unrestricted base MLT import, or a way to bypass approval/policy/validation. Large outputs should be links/resources with expiry rather than inline blobs.

MCP calls use the caller's scoped API token and the same idempotency, quota, state, and audit rules as the web app. An agent cannot mark a failed inspection successful; it can submit a review decision that the backend validates and attributes.

## 16. Testing and evaluation strategy

### 16.1 Preserve and extend deterministic tests

The current suite is a good unit-test seed. During this audit, 37 tests passed and the single end-to-end `melt` transform regression was skipped because `melt` is not installed in WSL.

Add:

- typed-schema tests for every operation, accepted field, unknown field, numeric boundary, oversized value, and schema migration;
- property/fuzz tests for rational timestamp conversions and random nonoverlapping timelines;
- normalization tests for chained trim/split/remove/insert/reorder, generated ID collisions, overruns, removed targets, and deterministic output;
- path/security tests for absolute paths, `..`, symlinks, Unicode, protocols, MLT resources, XML size/entities, and hostile filenames;
- process-supervisor tests for timeout, no-progress, cancellation, worker death, huge logs, process-tree kill, disk full, and partial cleanup;
- state-machine/idempotency tests including duplicate messages, stale leases, concurrent approval, and plan revision races.

### 16.2 MLT compiler tests

- One golden canonical XML fixture per certified feature and meaningful combination. Canonicalize XML before comparison to avoid irrelevant attribute formatting churn.
- Parse emitted XML and assert all references, frame in/out values, services, properties, resources, and track routing.
- Run feature micro-projects in the exact pinned production image and inspect output; never skip the required production CI lane.
- Maintain a generated capability manifest keyed by compiler and toolchain image versions.
- Add explicit tests proving each schema field changes—or intentionally does not change—the generated MLT. This catches the current accepted-but-ignored fields.
- Keep conservative base-project tests separate; do not block initial service launch on supporting arbitrary base projects.

### 16.3 Integration and render tests

Use tiny synthetic, deterministic assets generated during tests (color bars, numbered frames, tone/silence, moving geometry, stereo channel identifiers). Test probe -> validate -> normalize -> compile -> render -> inspect for:

- CFR and rational 30000/1001;
- representative VFR input and source-to-output mapping;
- all track/media types and supported operation combinations;
- missing/corrupt media, unsupported codec, truncated container, huge dimensions, and no audio/video;
- renderer crash/timeout/cancel/disk full;
- exact video-frame duration, bounded audio padding, monotonic PTS, and A/V sync;
- nonempty/nonblack/nonfrozen output and exact boundary behavior.

Golden video tests should compare metadata and selected decoded frames/audio windows with feature-appropriate tolerances (SSIM/perceptual hashes, OCR boxes, audio envelopes), not whole-file hashes, because encoders/toolchains can change bytes without changing behavior. Pin toolchains so meaningful changes are deliberate.

### 16.4 Instruction-following benchmark

Create `benchmarks/v1/manifest.jsonl` plus licensed/synthetic media. Start with 25-40 cases covering:

- exact timestamp trim and reorder;
- visual event trim (“keep the dunk and follow-through”);
- transcript/keyword edit;
- caption timing/style/placement;
- transform/reframing with subject containment;
- overlay, transition, audio fade/mix, speed, chroma/mask/filter only after certified;
- VFR, silent, portrait, screen recording, long/static, fast action, multiple assets/tracks;
- ambiguous request, impossible request, unsupported operation, and prompt injection in on-screen/spoken text.

Each case should include:

```text
case_id, license/source provenance, media digests, user instruction,
expected capability/unsupported classification, allowed assumptions,
hard temporal/structural constraints, semantic rubric,
reference analysis annotations, optional reference plan,
output checks and scoring tolerances
```

Score layers separately:

- planning validity and instruction coverage;
- temporal IoU/frame error for selected ranges;
- operation/parameter correctness;
- compilation/render success;
- output technical conformance;
- blinded semantic quality ratings for subjective edits;
- latency and model/render cost.

Do not require a single exact edit plan when multiple edits are valid. Use hard constraints plus rubrics. Version the dataset and evaluation code, freeze a holdout set, record model/prompt/toolchain versions, and compare against release thresholds. Convert every production failure and the existing blank-path, missing-transform, corrupt-output, and VFR-642 incidents into regression cases.

## 17. Detailed phased migration roadmap

The strategy is four product milestones. Existing CLIs and the Codex skill remain usable throughout; each phase produces something directly testable by the next group of users.

### Phase 0 — Codex skill -> standalone engine

**Objective:** make the current successful workflow run from normal Python code with no Codex harness.

**Files/modules:** add a small set of regression fixtures under `tests/fixtures/` and `tests/integration/`; add `video_editing/workspace.py`, `video_editing/supervisor.py`, `video_editing/artifacts.py`, `video_editing/analysis/`, `video_editing/planning/`, and a standalone orchestration entry point such as `video_editing/pipeline.py`/`video-edit-run`.

**Extract/change:** freeze a few representative examples, including the blank-path, missing-transform, corrupt-output, and VFR failures. Keep `edit-plan` 1.0. Add only the minimum workspace/process handling needed for safe local execution: job-local paths, unique outputs, fixed tool configuration, timeouts, bounded logs, cancellation cleanup, and result manifests. Move natural-language source analysis and plan generation from the Codex tool loop into provider-neutral Python interfaces, starting with one model provider and schema-constrained output of the existing IR.

**Unchanged:** `timebase.py`, the existing edit-plan shape, CLI compatibility, the deterministic `plan.py` -> `operations.py` -> `mlt.py` pipeline, and the Codex plugin as a working alternate frontend.

**Tests:** current unit suite, mandatory pinned-toolchain render smoke test, regression examples, mocked model failure/repair tests, path/process timeout tests, and at least a small instruction-following benchmark.

**Definition of done:** one Python command/function accepts `video + natural-language instruction` and produces a valid `edit-plan.json`, `project.mlt`, and validated rendered video without any Codex harness interaction.

### Phase 1 — Standalone engine -> API

**Objective:** wrap the standalone engine in the smallest durable local/cloud service.

**Files/modules:** add `service/app.py`, minimal route/models/repository modules, PostgreSQL migrations, a filesystem/S3 storage interface, and one background worker command.

**Extract/change:** implement `POST /videos`, `POST /edits`, `GET /edits/{id}/plan`, approval, render, job status, and result endpoints. Store video/edit/plan/approval/result records in PostgreSQL. Store bytes on the local filesystem in development and S3-compatible storage in cloud deployment. The single worker claims database jobs and calls the Phase 0 pipeline. Keep the public states to uploaded/analyzing/planning/awaiting_approval/approved/rendering/completed/failed.

**Unchanged:** standalone engine, existing IR, CLIs, and Codex skill. No Redis, distributed queue, microservices, or worker fleet yet.

**Tests:** API contract, database state transitions, upload/storage adapter parity, approval-before-render enforcement, restart recovery, one-worker claiming, and complete upload -> plan -> approve -> render -> result integration.

**Definition of done:** a normal API client can complete the entire workflow against PostgreSQL plus filesystem/S3, and the result endpoint can only return the uniquely validated output.

### Phase 2 — API -> usable website

**Objective:** put the workflow in front of real users and learn where it fails.

**Files/modules:** build the minimum frontend pages/components in the product web application for upload, prompt, plan review, approval, progress, playback/download, and feedback/failure reporting.

**Extract/change:** connect only to the Phase 1 API. Show the human plan and warnings, allow approval, poll or stream basic progress, play/download the accepted result, and collect structured failure reports plus optional user comments. Instrument the funnel from upload through accepted output.

**Unchanged:** engine, IR, API contract, single worker, and local viewer for development. Avoid building a complex timeline editor before users demonstrate that plan editing is necessary.

**Tests:** browser happy path, upload limits, failed analysis/planning/render presentation, approval enforcement, progress recovery after refresh, playback/download, basic accessibility, and feedback capture.

**Definition of done:** target users can independently upload a video, describe an edit, understand and approve the plan, see progress, play/download the result, and report a bad outcome. The team has real funnel, failure, latency, and cost data.

### Phase 3 — Harden and scale what users prove matters

**Objective:** invest in reliability, security, cost, and integrations based on observed demand and failures rather than speculative infrastructure.

**Files/modules:** add selectively: containerized render-worker image/runtime, resource policy/sandbox, queue and attempt/lease modules, retry/reconciliation logic, stronger auth/tenant controls, observability dashboards, usage/billing models, MCP adapter, and expanded benchmark/evaluation tooling.

**Extract/change:** prioritize from real evidence: containerized render workers; CPU/RAM/disk/PID/time limits; retries and cancellation; Redis or another queue when PostgreSQL polling is insufficient; worker concurrency/autoscaling; stronger upload/MLT isolation; structured logs/metrics/traces; storage lifecycle; model/frame-sampling cost optimization; authentication/rate limits; billing/quotas; thin MCP tools over the same API; and more sophisticated automated and human evaluations.

**Unchanged:** the standalone engine/API boundary, approved-plan flow, and existing edit-plan IR unless real plan data proves a version change is necessary.

**Tests:** add according to each hardening feature: malicious/corrupt media, timeout/OOM/disk-full, duplicate delivery and worker death, tenant isolation, load/soak, quota enforcement, model/prompt regressions, MCP contract/security, billing-meter accuracy, disaster recovery, and a larger benchmark release gate.

**Definition of done:** the measured reliability, security, latency, cost, and capacity targets for the current user tier are met; every added subsystem addresses a demonstrated bottleneck, incident class, customer need, or business requirement.

## 18. Immediate next five engineering tasks

1. **Freeze a few end-to-end regression examples.** Use a pinned MLT/FFmpeg environment and capture the current successful flows plus the blank-path, missing-transform, corrupt-output, and VFR failures.
2. **Build the minimum `JobWorkspace` and `ProcessSupervisor`.** Add job-local paths, unique outputs, fixed binaries, timeouts, bounded diagnostics, cancellation cleanup, and a result manifest without changing edit-plan 1.0.
3. **Extract source analysis into Python.** Produce bounded coarse/targeted frame evidence and optional transcript artifacts through a provider-neutral interface.
4. **Extract planning into Python.** Given the instruction and analysis artifacts, call one configured model and produce the existing `edit-plan.json`, with deterministic validation and a bounded structured repair loop.
5. **Add one standalone runner and prove the target flow.** A single function/CLI must run `video + instruction -> valid edit plan -> MLT -> rendered/validated video` with no Codex harness; only then begin the Postgres/API phase.

## Overall extraction difficulty

**Medium.** The repository already enforces the right high-level boundary and has a compact, dependency-free deterministic core, rational timing, strict allow-listing, MLT XML generation, structured errors, and useful tests. That avoids a rewrite. The difficulty is not moving Python functions behind HTTP; it is implementing and validating everything Codex currently supplies implicitly: semantic source analysis, model orchestration, plan provenance/approval, safe worker supervision, durable job/attempt state, tenant storage/isolation, and trustworthy output QA. Several compiler/schema fields also need correctness work, and MLT compatibility is not yet certified. An internal single-tenant service is achievable incrementally; a reliable private beta is materially more work because native media processing and probabilistic planning both require strong containment and evaluation.
