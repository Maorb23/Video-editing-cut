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
- `volume`, `fade_audio`, `audio_mix`, `audio_transition`
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

For strong hue remapping, set `tint_strength` from 0 through 1 and an opaque
`tint: "#RRGGBB"`. Use 0.75 for a strong requested color; zero bypasses hue
remapping. One generic `avfilter.colorize` mapping handles every hue and retains
source lightness (`av.mix=1`). Its catalog pins `Lavfi11.14.102`; rendering fails
explicitly on a different version. Strong grades are currently static and
unmasked. Existing grading/keyframe plans keep their legacy compilation path.
`tail_seconds: 5` replaces `target`, `start`, and `duration`: Python expands it
over the last five seconds of the resolved timeline, after pause edits.

`audio_transition` requires `kind` (`l_cut`, `j_cut`, or `crossfade`),
`from_clip_id`, `to_clip_id`, `picture_boundary_frame`, `av_offset_frames`, and
`crossfade_frames`. Picture clips must be adjacent on one audible video track.
L/J offsets are 2–6 frames; crossfade offsets are zero. Crossfades are limited to
60 ms rounded to project frames. L/J cuts also require an `evidence_id` matching
persisted `analysis.transition_safety`, including clip IDs, picture boundary,
confidence at least 0.8, `lips_visible_near_cut: false`, the matching
`l_cut_safe`/`j_cut_safe` flag, and `source_evidence_ids`. Missing evidence means
a synchronized edit. Python validates source handles and compiles separate
audio playlists while muting embedded audio exactly once. Conflicting explicit
mixing, duplicate source audio, speed, cleaning, and timed audio effects fail.

`parametric_eq.bands` contains 1–16 exact `{frequency, gain_db, q}` records.
`reverb` allow-lists room size, damping, wet/dry mix, and pre-delay. `dereverb`
references an immutable derived audio asset, the exact `deepfilternet3-local`
model name, and a SHA-256 model fingerprint; it never means noise reduction.

Silence calibration uses 50 ms RMS windows, the median of the lowest 20%, and
an 8 dB margin clamped to −60…−30 dBFS. Insufficient room-tone support or
separation falls back to the documented −50 dBFS threshold. `SilenceSettings`
configures these values, the phone-video default 0.25 second detection minimum,
and 0.12 second speech padding. Analysis always covers the probed source duration;
EOF closes at that duration.
`analysis/silence.json` persists calibration, settings, frame-exact candidates,
source fingerprints, analyzed duration/rate, evidence IDs, and available context;
`analysis/detected-silences.md` provides
the dedicated `## Detected silences` review section. No tiny window-level RMS
regions are listed. Confidence means calibration confidence, not measured speech
or lip-safety confidence.

The runtime draft emits `silence_decisions` records containing only
`candidate_id`, `asset_id`, and `action` (`keep`, `shorten`, `remove`). Python
re-evaluates policy, preserves padding, ripples all tracks together, and remaps
static effects. Short pauses below 0.35 s stay; medium and long pauses shorten;
verified non-speech pauses above 0.8 s may be removed. Reliable emphasis retains
more pause. Strong edits cannot exceed the persisted policy recommendation.
Unreliable context uses synchronized timing. Actual choices and reasons persist
in `analysis.silence_decisions` and the Decisions log, separately from detection.
Pause edits intersecting keyframes, speed, or cleaned audio require a separate
iteration and fail explicitly instead of silently changing those effects.
Missing, stale, partial, or differently configured evidence blocks pause planning
and requires preflight to be rerun before an iteration can be proposed.

Export V1 is MP4, `libx264`, and AAC. Optional deterministic settings include
video/audio bitrate, pixel format, and movflags; the standalone runner applies
those settings over its quality preset. Standalone jobs additionally reject
absolute/traversing asset paths and verify every declared SHA-256 fingerprint
against the current job-local media bytes.

Convert a rational seconds value `s` with `frames = round(s * numerator /
denominator)` only after profile selection. Do not use a decimal approximation
of 30000/1001.
