# AGENTS.md

## Project

This repository implements an agentic video-editing system consisting of:

1. A deterministic Python video-editing core and CLI.
2. A Codex plugin for translating natural-language editing requests into typed edit plans.
3. Shotcut-compatible MLT project generation.
4. Automated rendering, inspection, and iterative correction.

The long-term goal is for the editing core to remain reusable outside Codex and suitable for a future hosted SaaS.

## Reference Repository

The sibling repository:

`../manim_skill`

is a READ-ONLY architectural reference.

You may inspect it for:
- Codex plugin structure
- SKILL.md conventions
- Plan → Preflight → Render → Inspect → Iterate workflow
- viewer/review concepts
- testing conventions
- scripts and repository organization

Do NOT modify, delete, rename, format, commit, or otherwise alter files in `../manim_skill`.

Do not create dependencies on the Manim repository.

If code or assets are reused, verify license compatibility and preserve required attribution.

## Architecture

Maintain a strict separation between:

Natural-language / Codex layer
        ↓
Typed edit plan
        ↓
Validation
        ↓
Deterministic Python editing core
        ↓
MLT project
        ↓
melt / renderer

Codex must not be required for the deterministic editing engine to function.

Do not implement editing by generating arbitrary shell or FFmpeg commands directly from natural language.

## Edit Plans

Codex produces a versioned structured edit-plan JSON document.

Python code validates and executes it.

Unknown operations or unsupported properties must fail explicitly rather than being silently ignored.

Timing must be frame-accurate and rational frame rates such as 30000/1001 must be preserved.

## Safety / Immutability

Never modify source media.

Never overwrite an existing user-provided MLT project.

Generated artifacts belong inside the working project/job directory.

Prefer deterministic, inspectable behavior over clever implicit behavior.

## Video Editing Scope

Supported functionality should be implemented incrementally and tested.

V1 targets:
- cuts / trims / splits
- reorder
- multiple audio/video tracks
- captions
- overlays
- transitions
- transforms
- audio mixing
- fades
- keyframes
- speed changes
- chroma key
- masks
- curated filters

Do not attempt arbitrary Shotcut functionality.

## Rendering

Shotcut is primarily an interactive editor for generated MLT projects.

Automated rendering should use MLT/melt.

FFmpeg/FFprobe may be used for probing, inspection, encoding support, and analysis where appropriate.

## Agent Workflow

For editing tasks follow:

Plan → Preflight → Analyze → Compile → Render → Inspect → Iterate

Preview renders and inspection may be performed autonomously.

Do not require user confirmation for normal non-destructive compilation, preview rendering, inspection, or iteration.

Require explicit approval before destructive operations or operations that would overwrite user-owned artifacts.

## Engineering

- Python 3.10+
- Prefer pathlib.Path
- UTF-8 explicitly
- Typed interfaces where useful
- Structured errors
- Nonzero CLI exit codes on failures
- list-form subprocess arguments
- deterministic tests
- no automatic dependency installation

Keep modules reusable so the same core can later run inside a hosted worker/API architecture.
