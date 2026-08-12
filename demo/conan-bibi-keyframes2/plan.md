# Conan and dog smooth keyframed reframing — attempt 2

## Goal and audience

Create a second, independently reviewable version of the meeting clip with smooth digital camera moves. Emphasize Conan and the dog at 3–5 seconds, pull back toward both men at 6–11 seconds, return to Conan at 22–29 seconds, and finish tightly on the dog.

## Profile and assets

- Source: `../assets/Conan_bibi.mp4` (primary picture and original stereo audio)
- Output profile: 1280×720 progressive, 25/1 fps, BT.709, 44.1 kHz stereo plan profile
- Preserve all 799 timeline frames (31.96 seconds at 25 fps).
- Never modify or overwrite the source media.

## Ordered timeline

1. Frames 0–74 (0.00–2.96 s): hold the full source composition.
2. Frames 75–125 (3.00–5.00 s): smoothly zoom and pan toward Conan on the right and the dog beside him, ending at a 160% crop.
3. Frames 126–149 (5.04–5.96 s): hold the Conan-and-dog crop.
4. Frames 150–275 (6.00–11.00 s): slowly pull back and shift left to a 125% two-men composition.
5. Frames 276–549 (11.04–21.96 s): hold the two-men composition while preserving the source camera changes.
6. Frames 550–600 (22.00–24.00 s): smoothly zoom toward Conan, the man on the right, ending at a 190% crop.
7. Frames 601–724 (24.04–28.96 s): hold the Conan crop.
8. Frames 725–798 (29.00–31.92 s): smoothly pan right and zoom to a 210% dog close-up through the final frame.

## Edit rationale

All reframing uses one continuous affine transform with paired hold keyframes. There are no added cuts, dissolves, or sudden geometry changes. The source contains its own camera cuts, especially around 16–22 seconds; those remain part of the original footage, while every newly added move stays smooth.

## Audio, captions, effects, and transitions

- Preserve the original audio without gain changes, fades, or replacement.
- Add no captions, overlays, filters, structural cuts, or synthetic transitions.
- Use geometry keyframes only.

## Export quality

- MP4, H.264/libx264, AAC, yuv420p, fast-start metadata.
- Render with Shotcut/MLT's final-quality preset.

## Review criteria

- Conan and the dog remain visible throughout the 3–5 second move and ensuing hold.
- The 6–11 second pull-back is gradual and ends with both men readable.
- Conan remains dominant from the completed 22-second move until 29 seconds.
- The final move lands on the dog without exposing black edges.
- Frames immediately before, at, and after every keyframe show continuous motion with no introduced jump.
- The original audio remains synchronized, intelligible, and unclipped.
