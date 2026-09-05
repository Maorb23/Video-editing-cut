# Video Search Keyframed Zoom

## Goal and audience

Create a polished version of `demo/assets/Video_search.mp4` that guides attention through three beats: the first typing moment, the lyric display, and the later typing moment. Preserve the source content and audio while using smooth, frame-accurate keyframed zooms.

The edit is intended for viewers who should be able to follow the interaction and read the on-screen lyrics without losing spatial context.

## Profile and assets

- Source role: sole picture and program-audio source.
- Output profile: 1920×1140 progressive at 30/1 fps, 48 kHz stereo. The source is variable-frame-rate (640 coded frames over about 22.25 seconds, with `r_frame_rate` 60/1 and `avg_frame_rate` 19200000/667463), so 30/1 is the deterministic CFR timeline closest to its measured average rate.
- Source media is immutable. All generated artifacts remain in this job directory.

## Ordered timeline

1. Frames 0–89: retain the full 1920×1140 screen recording.
2. Frames 90–105: zoom from full frame to the first browser address-bar typing area.
3. Frames 105–177: hold the address bar at `-37.5%/0%:175%x175%` while the first query is entered.
4. Frames 177–195: pull smoothly back to the full frame for the search results.
5. Frames 195–510: retain the full frame through result browsing and navigation.
6. Frames 510–540: zoom toward the left-side lyric column at `0%/-15%:145%x145%`.
7. Frames 540–585: hold the enlarged lyric framing for readability.
8. Frames 585–600: pull smoothly back to the full frame.
9. Frames 600–615: zoom toward the browser address bar again at `-37.5%/0%:175%x175%`.
10. Frames 615–639: hold on the second typed query.
11. Frames 639–654: pull smoothly back to the full frame.
12. Frames 654–668: end on the full frame. Total duration: 669 frames (22.3 seconds at 30/1 fps).

All listed keyframes use integer frames relative to the transform start. Transform interpolation is linear, as required by edit-plan V1.

## Edit rationale

The three zoom beats follow the viewer's likely attention rather than introducing cuts. Full-frame recovery between beats maintains orientation, while holds at the zoomed geometry make typing and lyrics legible.

## Audio, captions, effects, and transitions

- Preserve the original synchronized audio without added mixing or loudness changes.
- Add no captions or text overlays; the existing on-screen lyrics remain part of the source image.
- Use one curated transform operation with frame-accurate geometry keyframes.
- Add no cuts, transitions, speed changes, or unrelated filters.

## Export quality

- MP4 container, H.264 video, AAC audio.
- Final-quality render using the repository's deterministic MLT/melt pipeline.
- Preserve the source duration and avoid source-media modification.

## Review criteria

- The output begins and ends at the full source framing.
- Each of the three attention beats is visible in order: typing, lyrics, typing.
- Zooms are smooth and do not expose black edges.
- The focal content remains inside the frame and is materially easier to see at each hold.
- Audio remains present, synchronized, and free of edit-induced discontinuities.
- Every transform keyframe has persisted before/at/after frame evidence and passes the inspection gate.
