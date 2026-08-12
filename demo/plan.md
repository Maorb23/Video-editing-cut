# LeBron dunk highlight

## Goal

Create a short highlight centered on LeBron James's main tomahawk dunk. Remove
the long half-court setup and the YouTube end card while preserving the drive,
finish at the rim, and immediate follow-through.

## Source and profile

- Source: `assets/Lebron_dunk.mp4` (read-only)
- Source duration: 30.962358 seconds; 925 video frames
- Profile: 1280×720 progressive, 30000/1001 fps, BT.709
- Audio: preserve the original stereo AAC track at 44.1 kHz

## Timeline

Use source frames 345–683 inclusive, corresponding to approximately
11.5115–22.8228 seconds. The resulting highlight is 339 frames, or about
11.3113 seconds. This includes the approach, the rim-camera dunk, and the
immediate finish, but excludes the end card.

Add the caption `King james` from timeline frame 180 through frame 314
(approximately 6.006–10.477 seconds into the highlight). Place it in a compact
upper-right title treatment so it does not cover the ball, rim, players, or the
broadcast scoreboard. Use gold text on a translucent black background.

## Export and review

Generate an editable `project.mlt` and render H.264/AAC MP4 when MLT `melt` is
available. Inspect regular samples and frames surrounding the trim and caption
boundaries. Confirm that the dunk is complete, the caption is readable, the
source audio remains continuous, and no end-card footage remains.
