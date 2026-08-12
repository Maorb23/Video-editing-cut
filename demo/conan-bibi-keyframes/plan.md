# Conan, Bibi, and dog keyframed reframing

## Goal and audience

Create a polished reframing of the supplied meeting clip for general viewing. Begin with visual emphasis on Conan (right), smoothly pull back to reveal the original scene, then settle into a restrained group framing that keeps Bibi (left) and the dog (right) readable.

## Profile and assets

- Source: `../assets/Conan_bibi.mp4` (primary video and original stereo audio)
- Output profile: 1280×720 progressive, 25/1 fps, BT.709, 44.1 kHz stereo
- Preserve the source duration and use integer-frame timing throughout.
- Do not modify or overwrite the source.

## Ordered timeline

1. Frames 0–74 (0.00–2.96 s): hold a 170% crop positioned on Conan at the right side of the table.
2. Frames 75–174 (3.00–6.96 s): keyframe a smooth zoom/pan outward from Conan to the full 1280×720 composition.
3. Frames 175–224 (7.00–8.96 s): hold the full composition so the spatial relationship between Bibi, Conan, and the dog is clear.
4. Frames 225–299 (9.00–11.96 s): gently move to a 108% centered group crop, keeping Bibi at left and the dog at right in view.
5. Frame 300 through end: hold the Bibi-and-dog group framing. Existing source-camera cuts remain intact.

## Edit rationale

The opening crop gives Conan clear priority. The four-second pull-back is deliberately slow enough to read as a motivated camera move rather than a sudden digital zoom. The final crop is intentionally subtle because Bibi and the dog occupy opposite sides of the source image; a tighter crop would cut off one of them.

## Audio, captions, effects, and transitions

- Preserve the original audio without gain changes, fades, or replacement.
- Add no captions, overlays, filters, or synthetic transitions.
- Use one affine transform with linear geometry keyframes; preserve the source's own cuts.

## Export quality

- MP4, H.264/libx264, AAC, yuv420p, fast-start metadata.
- Render the final deliverable using the skill's final-quality preset.

## Review criteria

- Conan's face and upper body remain inside frame during the opening crop.
- The pull-back contains no black edges, jumps, or visible geometry discontinuities.
- At the final hold, Bibi and the dog both remain visible at the left and right edges respectively.
- Source audio stays synchronized, intelligible, and free of clipping introduced by the edit.
- Output duration and 25 fps timebase match the plan.
