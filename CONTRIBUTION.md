# Contributing

Use Python 3.10 or newer and keep the core free of Codex dependencies. Changes
to edit-plan behavior need deterministic unit tests. Never make the natural-
language layer emit or execute arbitrary shell or FFmpeg command fragments.

Run the complete suite with:

```bash
python -m unittest discover -s tests -v
```

Integration tests may use locally installed `melt`, `ffmpeg`, and `ffprobe`,
but unit tests must not install or require them. Record manual Shotcut smoke
tests with exact versions in `plugins/video-editing-skill/skills/video-editing-skill/references/compatibility.md`.

