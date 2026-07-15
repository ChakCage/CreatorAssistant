# Shorts render benchmark — 2026-07-15

## Test material

- Duration: 60 seconds.
- Source: reproducible FFmpeg `testsrc2`, 1280×720, SDR, AAC audio.
- Frame rate: `60000/1001` (59.94 FPS).
- Output: 1080×1920 MP4, H.264 NVENC (`p4`), AAC.
- Subtitle position: lower third, center at Y=1450 (75.5% of 1920).
- Subtitle text: long Russian sentence plus an unbroken long Russian word.

## Results

| Layout | Subtitle style | Wall time | FFmpeg speed |
|---|---:|---:|---:|
| Center Crop | Clean | 7.391 s | 8.63× |
| Center Crop | Large | 7.265 s | 8.79× |
| Center Crop | Gaming | 7.250 s | 8.78× |
| Blur Background | Clean | 11.375 s | 5.46× |
| Blur Background | Large | 11.438 s | 5.45× |
| Blur Background | Gaming | 11.750 s | 5.29× |
| Solid Color | Clean | 7.218 s | 8.87× |
| Solid Color | Large | 7.125 s | 8.91× |
| Solid Color | Gaming | 7.063 s | 9.03× |

FFprobe confirmed 1080×1920 H.264 output with `avg_frame_rate=60000/1001`; the render service also confirmed AAC audio and the expected duration for every result.

## Player smoke test

The same 59.94 FPS source was opened through the real `QMediaPlayer` used by `CandidateEditor`:

- media loaded with a 60,000 ms duration;
- a real mouse press/move/release drag scrub succeeded;
- seek to 1,250 ms succeeded;
- +0.1 second step succeeded;
- playback paused automatically at the 2,000 ms candidate boundary;
- the timeline finished at 2,000 ms.

## Bottleneck

The confirmed remaining bottleneck is the Blur Background filter branch: it requires background scale, blur, upscale, foreground scale and overlay. Moving blur work from the full 1080×1920 frame to a 270×480 copy keeps the complete 59.94 FPS render at 5.3–5.5× real time. Solid Color uses FFmpeg's native color source at the exact input frame rate and is as fast as Center Crop. Subtitle style has comparatively little effect. No full-size intermediate video is created.

## Real Minecraft sample

A five-second 2560×1440/60 FPS Minecraft source was rendered with Blur Background, Gaming subtitles and the lower-third position. Render time was 2.28 seconds. Visual inspection confirmed that the subtitle block is below the central gameplay layer and does not cover the crosshair, terrain or HUD.
