# Shotcut-Compatible Video Editing Skill

This repository contains a local, deterministic Python editing engine and a
Codex plugin that turns natural-language requests into versioned JSON edit
plans. The Python core validates plans, generates editable Shotcut/MLT
projects, renders MP4 through `melt`, and creates immutable inspection passes.
Codex is not required to run any core command.

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

No command installs dependencies. Use `check_environment.py` to discover
Shotcut's bundled `melt`, standalone MLT, FFmpeg, and FFprobe.

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

