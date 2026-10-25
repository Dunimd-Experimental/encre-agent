---
name: video
description: Video file processing - inspect media info, convert formats, extract audio, extract frames, compress, trim, screenshot a frame, transcribe speech
aliases: [video-processing, video-file]
when_to_use: ".mp4 .avi .mov .mkv .webm .flv .m4v"
argument_hint: "[path to video file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## Video File Processing

You are processing a video file: **{{args}}**

### When to Use
- Inspect a video's properties (duration, resolution, codec, bitrate)
- Convert a video to another format/codec
- Extract the audio track from a video
- Extract frames (a still image per N seconds) for visual review
- Compress a large video, trim a clip, or grab a screenshot at a timestamp
- Transcribe speech in the video's audio

### When NOT to Use
- **A single still image** -> use the `images` skill
- **Audio-only files** -> use the `audio` skill
- **Live screen interaction / clicking UI** -> use `computer` / `computer_use` tools, not video processing
- **Streaming / webcam capture** -> these tools work on files; capture first, then process

### Processing Workflow
1. **Inspect first** -> get media info (duration, resolution, codec) before any conversion. This decides whether a transcode or trim is even needed.
2. **Choose the operation:**
   - Properties -> `action: info`
   - Convert format/codec -> `action: convert` (set output_path)
   - Pull the audio out -> `action: extract_audio` (then hand off to the `audio` skill)
   - Still frames for review -> `action: extract_frames` (set output_dir, fps)
   - Shrink the file -> `action: compress` (set quality; lower = smaller)
   - Cut a segment -> `action: trim` (start + duration)
   - Grab one frame -> `action: screenshot` (set time)
   - Speech to text -> `action: transcribe`
3. **Cost awareness** -> video conversion and frame extraction are slow and disk-heavy; do them once, write outputs to files, and read those files rather than re-running.
4. **Verify** -> after convert/trim/compress, re-check `info` on the output to confirm the result.
5. **Transcription** -> `transcribe` runs on the audio track; for long videos consider extracting audio first, then transcribing in segments.

### Tool Selection
- `media` tool (registered): `action: info` / `convert` / `extract_audio` / `extract_frames` / `compress` / `trim` / `screenshot` / `audio_info` / `transcribe` - primary path for video files
- `terminal` + `ffmpeg`/`ffprobe`: fallback only if the `media` tool is unavailable; the tool wraps ffmpeg
- `images` skill: process extracted frames as images (OCR, visual understanding)
- `audio` skill: handle an extracted audio track (transcribe, convert, info)

### Best Practices
- Always `info` first - duration and codec decide whether conversion is worth it
- For "understand what is in this video", extract frames at 1fps and visually read a sample, plus transcribe the audio - do not try to process the raw video stream
- Compress with a sane quality (18-23 is a good range; lower = higher quality + larger file)
- Trim using start + duration, not end timestamp; verify with `info` on the output
- Write outputs (frames, audio, transcripts) to a clear output directory and list them afterward

### Common Pitfalls
- **Re-running expensive ffmpeg passes** -> run once, save outputs, read from files; do not re-convert
- **Compressing with extreme quality** -> quality too low destroys the video; stay in 18-28 unless size is critical
- **Forgetting to set output_path** -> many actions need an explicit output path; omitting it fails or writes to unexpected places
- **Extracting too many frames** -> 1fps on a 10-minute video is 600 frames; sample at a lower fps or trim first
- **Transcribing long videos in one pass** -> extract audio, then transcribe in segments to avoid timeouts

### Pairing with Other Tools
- `terminal` (ffmpeg/ffprobe) - all video operations
- `images` - process extracted frames (OCR, visual read)
- `audio` - process the extracted audio track (transcribe, convert)
- `file_write` - save a transcript or frame list
- `file_read` - read a saved transcript or frame index
