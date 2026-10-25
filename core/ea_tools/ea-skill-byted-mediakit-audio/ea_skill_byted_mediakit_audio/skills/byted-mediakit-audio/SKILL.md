---
name: byted-mediakit-audio
description: Split vocals from background audio (separate-voice) and read audio container/stream metadata (probe-audio-metadata) through Volcengine AI MediaKit's mediakit-cli. Use for karaoke stems, dubbing prep, or inspecting sample rate, channels, codec, and duration.
---

# MediaKit Audio

Drive the `audio` domain of `mediakit-cli` to produce vocal/background stems or to inspect an audio file's container and stream metadata. Commands follow `mediakit-cli [--cloud|--local] audio <tool> [flags]`; cloud tools are asynchronous and return a `task_id` that `shared query-task` resolves.

## When to Use

- Separating a song, interview, or video soundtrack into a clean vocal track and a background track (karaoke, dubbing, remixing).
- Checking an audio file's sample rate, channel count, bitrate, codec, and duration before deciding how to process it.
- You already have a public URL, a `mediakit://` file id, or a local path and want Volcengine's audio capabilities rather than local FFmpeg.
- Batch jobs where you need to confirm the exact parameter surface first (`--schema`) and keep retries idempotent (`client_token`).

## When NOT to Use

- Transcoding, trimming, extracting frames/audio, compressing, or transcribing a local file → use the `media` tool (local FFmpeg, no API key).
- Speech-to-text or speech translation → use `transcribe_audio` / `translate_audio`.
- Music generation, TTS, or voice cloning → use the dedicated generation skills (`byted-music-generate`, `byted-text-to-speech`), not this one.
- No `MEDIAKIT_API_KEY` and no cloud access → stop and report the missing prerequisite instead of guessing results.

## Prerequisites

- Install: `npx @volcengine/mediakit-cli install -y` (updates: `npm install -g @volcengine/mediakit-cli`); verify with `mediakit-cli --version`.
- Non-interactive init: `mediakit-cli init --api-key <key> --credential-store config --yes`.
- Auth comes from `MEDIAKIT_API_KEY` (console: <https://console.volcengine.com/imp/ai-mediakit/settings>), optionally `MEDIAKIT_ENDPOINT`, `MEDIAKIT_OUTPUT_PATH`.
- When this skill calls the CLI, set `MEDIAKIT_SURFACE=skill` explicitly; do not rely on an inherited value.
- Read `reference/shared.md` first — it covers mode switching, async polling, idempotency, error shapes, and local/cloud input rules.

## Tools in this skill

| Tool | What it does | Key parameters | Reference |
|------|--------------|----------------|-----------|
| `separate-voice` | Separates vocals from background audio into two independent audio tracks. Cloud only, asynchronous. | `video_url?` or `audio_url?` (one required; `video_url` wins), `callback_args?`, `client_token?` | `reference/separate-voice.md` |
| `probe-audio-metadata` | Returns container-level information and audio stream metadata. | `audio_url` (required), `callback_args?`, `client_token?` | `reference/probe-audio-metadata.md` |

`client_token` is limited to 64 characters; reuse the same value when retrying the same request and use a fresh one for a deliberate re-run. `callback_args` is opaque data echoed back through callbacks.

The CLI's audio domain also exposes voice-activity detection and audio transcoding. This skill does not document them; run `mediakit-cli audio --help` when a request needs them.

## Workflow

1. Read `reference/shared.md`, then confirm auth and mode with `mediakit-cli config show`.
2. Inspect the real parameter surface: `mediakit-cli audio <tool> --schema` (add `--local` for the local result shape).
3. Choose the mode: default is `cloud-first`; `--cloud` / `--local` override a single command and are mutually exclusive.
4. Submit the task, keep the returned `task_id`, and poll with `mediakit-cli shared query-task --task-id <id>` (or `--poll-complete` to block until terminal).
5. Report the produced file paths plus the metadata that matters (duration, format, sample rate).

## Example

```shell
# Separate vocals from a local file (uploaded automatically as mediakit://)
MEDIAKIT_SURFACE=skill mediakit-cli --cloud audio separate-voice \
  --audio-url ./demo.mp3 \
  --client-token sep-20261024-001

# Resolve the asynchronous task
mediakit-cli shared query-task --task-id <task_id> --poll-complete

# Inspect an audio file before processing it
MEDIAKIT_SURFACE=skill mediakit-cli audio probe-audio-metadata \
  --audio-url https://example.com/sample.wav

# Authoritative parameter list for the installed CLI version
mediakit-cli audio separate-voice --schema
```

## Best Practices

- Probe metadata first for long files; separation cost scales with duration.
- Pass `--output-path` explicitly (priority: `--output-path` > `MEDIAKIT_OUTPUT_PATH` > config > `~/.mediakit/temp`) so later steps can reference deterministic paths.
- Prefer public URLs or `mediakit://` file ids in cloud mode to avoid re-uploading large media.
- Report which mode ran, the task id, and both output stems.

## Common Pitfalls

- Omitting `MEDIAKIT_SURFACE=skill`, which makes the request report as a plain CLI call.
- Calling `shared query-task` under `--local`: it is cloud only.
- Passing both `--local` and `--cloud` — the CLI rejects the combination.
- Sending `video_url` to `probe-audio-metadata`; it accepts `audio_url` only.
- Local mode without `ffmpeg`/`ffprobe`; the CLI returns an `install_guide` — install it rather than retrying.
- Treating a cloud error object as success: cloud mode passes the raw API error through without extracting `message`.
- Assuming AAC is the only output: the underlying API accepts `output_format` (`mp3`, `wav`, `m4a`, `flac`), but the CLI flag must be confirmed with `--schema` for your version.

## Anti-patterns

- Inventing flags (`--url`, `--input`) instead of reading `--schema`.
- Declaring success on a `task_id` without polling to a terminal state.
- Resubmitting new tasks to "retry", burning quota and creating duplicate artifacts.
- Leaking internal execution metadata into user-facing result descriptions.

## Sources

- AI MediaKit Skill user guide: <https://docs.volcengine.com/docs/6448/2298695>
- Background-sound separation API (async, `task_id`, AAC default): <https://www.volcengine.com/docs/6448/2386113>
- mediakit-cli repository and README: <https://github.com/volcengine/mediakit-cli>

## References

- `reference/shared.md` — prerequisites, auth, mode overrides, async tasks, idempotency, error shapes.
- `reference/separate-voice.md` — full parameter and response detail for vocal separation.
- `reference/probe-audio-metadata.md` — full parameter and response detail for metadata probing.
