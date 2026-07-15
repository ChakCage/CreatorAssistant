# Shorts render benchmark — 2026-07-15

## Test material

- Duration: 60 seconds.
- Source: reproducible FFmpeg `testsrc2`, 1280×720, SDR, AAC audio.
- Frame rate: `60000/1001` (59.94 FPS).
- Output: 1080×1920 MP4, H.264 NVENC (`p4`), AAC.
- Subtitle position: lower third, center at Y=1260 (65.6% of 1920).
- Subtitle text: long Russian sentence plus an unbroken long Russian word.

## Results

| Layout | Subtitle style | Wall time | FFmpeg speed |
|---|---:|---:|---:|
| Center Crop | Clean | 7.031 s | 9.04× |
| Center Crop | Large | 7.062 s | 9.00× |
| Center Crop | Gaming | 7.078 s | 8.95× |
| Blur Background | Clean | 10.406 s | 5.98× |
| Blur Background | Large | 10.531 s | 5.91× |
| Blur Background | Gaming | 11.266 s | 5.51× |

FFprobe confirmed 1080×1920 H.264 output with `avg_frame_rate=60000/1001`; the render service also confirmed AAC audio and the expected duration for every result.

## Player smoke test

The same 59.94 FPS source was opened through the real `QMediaPlayer` used by `CandidateEditor`:

- media loaded with a 60,000 ms duration;
- seek to 1,250 ms succeeded;
- +0.1 second step succeeded;
- playback paused automatically at the 2,000 ms candidate boundary;
- the timeline finished at 2,000 ms.

## Bottleneck

The confirmed remaining bottleneck is the Blur Background filter branch: it requires background scale, blur, upscale, foreground scale and overlay. Moving blur work from the full 1080×1920 frame to a 270×480 copy reduced the cost enough to keep the complete 59.94 FPS render at 5.5–6× real time. Subtitle style has comparatively little effect. No full-size intermediate video is created.
