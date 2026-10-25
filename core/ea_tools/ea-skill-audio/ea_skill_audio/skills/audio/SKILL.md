---
name: audio
description: Audio file processing - inspect audio info, convert formats, transcribe speech to text, extract audio from video
aliases: [audio-processing, sound]
when_to_use: ".mp3 .wav .flac .aac .ogg .m4a"
argument_hint: "[path to audio file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## Audio File Processing

You are processing an audio file: **{{args}}**

### When to Use
- Inspect audio properties (duration, codec, bitrate, sample rate)
- Convert audio to another format (mp3, wav, flac, aac)
- Transcribe speech in an audio file to text
- Extract audio from a video file (then process it here)

### When NOT to Use
- **Video files** -> use the `video` skill; if you only need the audio, extract it first with `extract_audio`, then come here
- **A video frame image** -> use `images`
- **Generating speech (TTS)** -> these tools are for processing existing audio, not synthesizing it
- **Audio editing / mixing / effects** -> use `terminal` with ffmpeg/sox for advanced editing; the common actions are convert/transcribe/inspect

### Processing Workflow
1. **Inspect first** -> get audio info (duration, codec, bitrate) before converting or transcribing. Long files change your transcription strategy.
2. **Choose the operation:**
   - Properties -> `action: audio_info` (or `ffprobe` via terminal)
   - Convert format -> `action: convert` (set output_path, format, codec, bitrate)
   - Speech to text -> `action: transcribe`
   - Audio from a video -> `video` skill `extract_audio` first, then process here
3. **Transcription strategy** -> for long audio, the first pass may be slow; consider converting to a standard format (wav/mp3) first if the source is unusual, then transcribe.
4. **Verify** -> after convert, re-check `audio_info` on the output; after transcribe, spot-check the transcript against the audio length.
5. **Output** -> write transcripts to a file with `file_write` so the user can review; do not just dump a long transcript into chat.

### Tool Selection
- `media` tool (registered): `action: audio_info` / `convert` / `transcribe` (and `extract_audio` when the source is a video) - primary path for audio files
- `terminal` + `ffmpeg`/`ffprobe`: fallback only if the `media` tool is unavailable; the tool wraps ffmpeg
- `video` skill: `extract_audio` when the source is a video
- `file_write`: save transcripts and converted audio paths

### Best Practices
- `audio_info` first - duration and codec decide your path
- For transcription, normalize to a common format (16kHz mono wav often transcribes best) if the source is unusual
- Save transcripts to files; long transcripts flood the chat and are hard for the user to reuse
- When converting for size, pick a sensible bitrate (128-192 kbps for mp3 is a good default for speech)
- Keep the original; write conversions to a new path

### Common Pitfalls
- **Transcribing a very long file in one pass** -> may time out or lose accuracy; split or segment first
- **Unusual codec with no fallback** -> if transcription fails, convert to wav/mp3 first and retry
- **Overwriting the original** -> write conversions to a new path
- **Dumping a long transcript into chat** -> write it to a file and summarize; the user does not need 10,000 words inline

### Pairing with Other Tools
- `terminal` (ffmpeg/ffprobe) - all audio operations
- `video` - extract audio from a video source
- `file_write` - save transcripts
- `file_read` - read a saved transcript for follow-up questions
- `grep` - search within a transcript for a term or timestamp
