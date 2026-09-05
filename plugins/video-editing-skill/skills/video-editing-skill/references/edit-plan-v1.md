# Edit plan 1.0

The root contains exactly `version`, `profile`, `assets`, `tracks`,
`operations`, `export`, and optional `transcript`/`analysis` references.

The fixed profile contains positive integer `width`, `height`, `sample_rate`,
`channels`, and `frame_rate: {numerator, denominator}`. Optional `progressive`
and `colorspace` values map to the MLT profile.

Each asset has a stable `id`, a path relative to the plan where practical,
`kind` (`video`, `audio`, or `image`), positive `duration_frames`, a SHA-256
`fingerprint`, and optional raw `probe` metadata.

Each ordered track has `id`, `kind` (`video` or `audio`), and `clips`. A clip
has `id`, `asset_id`, nonnegative `timeline_start`/`source_in`, and positive
`duration`, all in frames. Clips on one track cannot overlap.
Video tracks preserve embedded audio unless `muted` is true. Audio tracks hide
video. Separate audible tracks mix explicitly, so duplicating the same source
on two unmuted tracks also duplicates its sound.

Transform geometry is an output rectangle, not a crop rectangle. A centered
1.5× zoom is `-25%/-25%:150%x150%`; dimensions below 100% shrink the image.
Geometry transform intervals on the same clip must not overlap. Opacity-only
fades may overlap geometry. Audio fade endpoints are linear amplitudes 0–1;
the compiler converts them to MLT decibels (zero maps to −90 dB).

Every operation has stable `id`, known `type`, optional `enabled`, and the
fields allow-listed for its type. Effects usually target a clip ID and may
have a frame `start` and `duration`. Structural operations resolve in array
order. Supported types are:

- `trim`, `split`, `remove`, `insert`, `reorder`
- `transition`, `caption`, `overlay`, `transform`
- `volume`, `fade_audio`, `audio_mix`
- `speed`, `chroma_key`, `mask`, `filter`
- `color_grade`, `parametric_eq`, `reverb`, `dereverb`

`transform.keyframes` contain strictly increasing integer `frame` values,
relative to the transform operation start, and geometry or opacity. Transform
interpolation is linear in V1; other interpolation values fail validation.
`filter.name` and its properties must exist in the curated registry. Unknown
fields fail validation.

`color_grade` supports `tint`, `brightness`, `contrast`, and `saturation`, plus
strictly increasing keyframes for those values. Interpolation is linear. Its
optional mask contains only a job-relative `resource`, `softness`, and `invert`.

`parametric_eq.bands` contains 1–16 exact `{frequency, gain_db, q}` records.
`reverb` allow-lists room size, damping, wet/dry mix, and pre-delay. `dereverb`
references an immutable derived audio asset, the exact `deepfilternet3-local`
model name, and a SHA-256 model fingerprint; it never means noise reduction.

Silence evidence records FFmpeg settings, rational project rate, and rounded
frame intervals. Defaults are −50 dB, 0.5 seconds minimum, 0.12 seconds speech
padding, and short 0.04-second audio fades at joins. Linked audio/video segments
must retain identical source and timeline ranges.

Export V1 is MP4, `libx264`, and AAC. Optional deterministic settings include
video/audio bitrate, pixel format, and movflags; the standalone runner applies
those settings over its quality preset. Standalone jobs additionally reject
absolute/traversing asset paths and verify every declared SHA-256 fingerprint
against the current job-local media bytes.

Convert a rational seconds value `s` with `frames = round(s * numerator /
denominator)` only after profile selection. Do not use a decimal approximation
of 30000/1001.
