---
name: tool-media
description: "ffmpeg-backed audio/video work: probe metadata, re-encode, extract audio or frames, compress, trim, grab a frame, or transcribe speech."
hidden: true
context: inline
---

# tool-media

`media` drives ffmpeg/ffprobe for the whole audio/video workflow: inspect metadata (`info`, `audio_info`), re-encode (`convert`), rip the audio track (`extract_audio`), dump frames as PNG (`extract_frames`), re-compress with a CRF target (`compress`), clip a range (`trim`), capture one frame (`screenshot`), and turn speech into text (`transcribe`). One call does one operation on one source file.

## When to Use

- Learn duration, codecs, resolution and bitrate before processing: `action="info"` or `"audio_info"`.
- Convert container/codec for playback or upload: `action="convert"` with `codec` and `bitrate`.
- Pull a podcast/interview track out of a video: `action="extract_audio"` with `format`.
- Build a thumbnail or storyboard: `action="screenshot"` at a timestamp, or `extract_frames` at an `fps`.
- Shrink a large file with predictable quality: `action="compress"` with `quality` (CRF).
- Cut a highlight: `action="trim"` with `start` and `duration`.
- Get a transcript of a short clip: `action="transcribe"`.

## When NOT to Use

- Static images (resize, OCR, convert) — use `image`.
- Generating or editing images/video creatively — use `generate_image` / `edit_image`.
- Long-form speech-to-text where a dedicated ASR service is available — `transcribe` loads Whisper locally and is slow.
- Live capture or streaming — unsupported.
- API-based transcription with language hints — use `transcribe_audio` from `media-api`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `info`, `audio_info`, `convert`, `extract_audio`, `extract_frames`, `compress`, `trim`, `screenshot`, `transcribe`. |
| `file_path` | string | yes | Source audio or video file. |
| `output_path` | string | no | Destination artifact for `convert`, `extract_audio`, `compress`, `trim`, `screenshot`. |
| `codec` | string | no | Video codec for `convert` (`libx264`, `libx265`, `libvpx`, ...). |
| `bitrate` | string | no | Target bitrate for `convert`/`compress` (`1M`, `500k`). |
| `format` | string | no | Audio format for `extract_audio`: `mp3` (default), `aac`, `wav`, `ogg`, `flac`. |
| `output_dir` | string | no | Directory for PNG frames from `extract_frames`. |
| `fps` | integer | no | Frame rate for `extract_frames`; default 1. |
| `quality` | integer | no | CRF for `compress`, 0–51, lower is better; default 23. |
| `start` | integer | no | Start time in seconds for `trim`. |
| `duration` | integer | no | Clip length in seconds for `trim`; default 10. |
| `time` | string | no | Timestamp for `screenshot`, e.g. `00:01:30`; default `00:00:01`. |

### Parameter Notes

- Output-producing actions need somewhere to write: pass `output_path` (or `output_dir` for frames) or accept the default location next to the source.
- `quality` is CRF, not a percentage — 18 is near-lossless, 28 is visibly soft, 23 is the default balance.
- `start` and `duration` are integer seconds only; sub-second trims are not expressible.
- `extract_frames` ignores `output_path` and uses `output_dir`.
- `fps` is the sampling rate for extraction, not the output frame rate of `convert`.

## Example

```json
{
  "action": "trim",
  "file_path": "D:/recordings/standup.mp4",
  "start": 620,
  "duration": 45,
  "output_path": "D:/recordings/standup-highlight.mp4"
}
```

## Best Practices

- Probe first with `info`; codec, duration and dimensions determine every later flag.
- For size reduction, try `compress` with `quality=23` and compare against the original before going lower.
- Extract audio as `mp3` for sharing, `wav` when a downstream model needs lossless input.
- Keep `output_path` explicit so later steps do not have to guess the generated name.
- Use `screenshot` for a single thumbnail (cheap) and `extract_frames` only when you need many.

## Common Pitfalls

- ffmpeg/ffprobe are resolved from PATH, then the pip `static-ffmpeg` package, then downloaded to `~/.cache/encre` on first use — the first call can be slow or fail offline.
- `transcribe` needs the `openai-whisper` package and is dramatically slower than ffmpeg operations; do not mix it into a quick pipeline.
- A `trim` past the media duration yields an empty or failed output.
- Copying streams with an incompatible container fails; let `convert` re-encode instead of forcing `-c copy` semantics.
- Frame extraction at high `fps` over a long video can produce thousands of files.

## Anti-patterns

- Calling `transcribe` on an hour-long file when an API transcriber is available.
- Using `compress` with `quality=51` and expecting usable video.
- Extracting frames one `screenshot` call at a time to build a storyboard.
- Processing images through `media` instead of `image`.

## Pairing with Other Tools

- `image` — OCR or convert the frames this tool extracts.
- `transcribe_audio` — API-based speech-to-text with language hints.
- `archive` — bundle extracted frames or clips for delivery.
- `info` — present a media summary as a card.
