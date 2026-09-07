---
name: video-editing-skill
description: Plan, validate, compile, render, inspect, and iteratively correct local video edits as typed JSON and Shotcut-compatible MLT. Use when Codex needs to cut or reorder media, build multitrack timelines, add captions/overlays/transitions/transforms/audio effects/keyframes/speed/chroma key/masks/curated filters, modify a supported MLT project conservatively, render MP4 with melt, or review edit-boundary and audio evidence.
---

# Video Editing Skill

Use this deterministic workflow:

`Plan → Preflight → Analyze → Compile → Render → Inspect → Iterate`

Resolve relative paths from this skill directory. Create each job in the
user's working directory, never inside this installed skill. Never modify
source media or overwrite a user-provided MLT project.

## Job contract

Maintain:

```text
<project>/
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

## Plan

Write `plan.md` before executable artifacts. Record the goal, audience,
profile, asset roles, exact ordered timeline, edit rationale, audio treatment,
captions, effects, transitions, export quality, and review criteria.

Read [references/edit-plan-v1.md](references/edit-plan-v1.md) before writing
`edit-plan.json`. Use integer frames for every executable time. Fix the
profile before converting seconds, and preserve its rational rate. Never put
arbitrary command fragments in the plan.

## Preflight and analyze

Run:

```bash
python3 scripts/check_environment.py --json
python3 scripts/probe_media.py <assets...> --output <project>/media-manifest.json
```

Do not install missing dependencies. Probe every input and carry paths,
metadata, duration, and SHA-256 fingerprints into `edit-plan.json`.

Semantic analysis is optional and provider-neutral. Use manual, local-model,
or future hosted adapter artifacts only when available. Read
[references/analysis-adapters.md](references/analysis-adapters.md) for their
interfaces. Do not require uploads or a cloud provider.

Validate before asking to compile:

```bash
python3 scripts/validate_edit_plan.py <project>/edit-plan.json --json
```

Correct every reported issue. Unknown operations/properties are errors, not
suggestions to ignore them.

## Confirmation gate

Show the human-readable plan, important media facts, timeline duration, and
planned outputs. Obtain explicit user confirmation before the first compile
or render. A subsequent compile/render that only implements already confirmed
corrections does not need a second confirmation unless scope materially
changes or a user-owned artifact might be overwritten.

## Compile and render

After confirmation, run:

```bash
python3 scripts/compile_project.py <project>/edit-plan.json --output <project>/project.mlt
python3 scripts/render_project.py <project>/project.mlt --output <project>/final.mp4 --quality final
```

Use `--base-project input.mlt` only after reading
[references/existing-mlt.md](references/existing-mlt.md). Always emit a new
project path. Refuse edits that intersect unsupported base structures.

Read [references/mlt-mappings.md](references/mlt-mappings.md) when using
filters or advanced effects and [references/compatibility.md](references/compatibility.md)
before claiming a Shotcut/MLT version is verified.

## Inspect

Generate a new immutable pass:

```bash
python3 scripts/inspect_video.py <project>/final.mp4 \
  --edit-plan <project>/edit-plan.json --output-dir <project>/review
```

Inspect every persisted regular sample and all frames immediately before, at,
and after each edit boundary and transform keyframe. Keyframe inspection claims
must cite the corresponding frame-exact entries in `keyframe_coverage` and the
persisted frame files; never infer coverage from playback or timestamp-based
seeking. Read the waveform plus silence, peak, volume, and loudness evidence.
For silence removal, persist detected intervals and settings before deriving
frame-exact linked A/V segments. Read adaptive calibration and policy suggestions
from `analysis/silence.json`; emit typed candidate decisions, never invented
noise floors, speech boundaries, frame measurements, lip visibility, or safety
evidence. Keep detected pauses in `## Detected silences`; Decisions contains only
actual edit choices. Use synchronized edits when reliable context is absent.
Only persisted matching safety evidence may authorize typed L/J cuts; Python
owns independent A/V routing and source-handle validation. Express all color
changes through `color_grade`: use `tint_strength: 0.75` for strong arbitrary
target hues and `tail_seconds` for final-interval intent. Never emit raw MLT/XML
or arbitrary filter properties. Inspect strong-hue conformance samples for hue
agreement and retained detail. For EQ/reverb/dereverb, inspect the structured
audio measurements and effect decisions. Dereverb requires the pinned optional
`deepfilternet==0.5.6` local runtime during preflight and an immutable derived
audio asset; stop when unavailable and never substitute noise reduction.
Complete every pending review record with a concrete observation, status,
severity, source action, and planned change.

Review every automated finding. Treat `effect_not_applied`, missing keyframe
coverage, a pending review, or a failed automated check as a pass blocker. Do
not mark or describe an inspection pass as successful unless its derived
eligibility is true after all persisted evidence is complete.

After completing the persisted reviews, enforce the pass gate with:

```bash
python3 scripts/inspect_video.py --check-record \
  <project>/review/passes/pass-NNN/inspection.json
```

Treat a successful render as execution evidence, not quality evidence. Fix
every high- and medium-severity finding, then compile, render, and inspect a
new numbered pass. Never edit an older pass.

Optionally offer browser review:

```bash
python3 scripts/video_viewer.py <project>/final.mp4 \
  --project <project>/project.mlt --plan <project>/plan.md
```

Use `--no-open` in headless environments.

## Deliver

Deliver clickable paths to `project.mlt`, `final.mp4`, `plan.md`,
`edit-plan.json`, `media-manifest.json`, `review/metadata.json`, and the latest
completed inspection record. State the actual profile/duration, exact tools
and versions used, validation performed, remaining low-severity findings, and
anything not verified on the current platform.
