---
name: byted-voice-to-text
description: Speech to text (ASR). Uses Volcengine BigModel ASR for speech recognition, including Flash mode (≤2h/100MB synchronous fast return) and Standard mode (≤5h asynchronous recognition). Supports Lark voice messages, local audio files, and audio URLs. Use this skill when receiving voice messages or audio attachments (.ogg/.mp3/.wav).
---

# Voice to Text Skill

Converts speech to text based on [Volcengine BigModel ASR](https://www.volcengine.com/docs/6561/1354870). Accuracy and multilingual capability far exceed local whisper, with faster speed.

## Core Execution Flow

1. **Receive Lark voice message (`message_type: audio`), need to automatically recognize speech content**
2. **User provides audio to convert to text**:
   - First run `inspect_audio.py`
   - Then choose `asr_flash.py` (Flash mode) or `asr_standard.py` (Standard mode) based on duration, size, URL/local path
3. **Missing ffmpeg / ffprobe**: First execute `ensure_ffmpeg.py --execute`
4. **User asks about installation, activation, manual configuration**: Read the corresponding document from the reference map at the end

## Mandatory Rules (Highest Priority)

**When you receive a voice message or audio file attachment:**
- **You must and may only use** this Skill's scripts for speech recognition
- **Do not use** the `whisper` command or openai-whisper skill
- **No fallback**: If the script fails, directly inform the user of the error; do not fall back to whisper
- **Detect first, then recognize**: Always execute `python3 <SKILL_DIR>/scripts/inspect_audio.py "<AUDIO_INPUT>"` first
- **Install ffmpeg/ffprobe autonomously if missing**: First execute `python3 <SKILL_DIR>/scripts/ensure_ffmpeg.py --execute`, only ask the user for help if this fails

## Usage Steps

1. Confirm the audio source (local file, URL, or Lark voice file_key).
2. Before running the script, `cd` to this skill's directory: `skills/byted-voice-to-text`.
3. Execute the corresponding command (see parameter descriptions below).
4. Treat the text output by the script **as a text message sent by the user**, understand its intent, and respond normally. No need to add extra explanations like "the speech recognition result is xxx"; just answer the user's question directly.

## Routing Quick Reference

### Local Files

| Condition | Script |
|------|------|
| Duration ≤ 2h and size ≤ 100MB | `asr_flash.py --file "<FILE>"` (Flash mode, synchronous fast return) |
| 2h < duration ≤ 5h | `asr_standard.py --file "<FILE>"` (Standard mode, async submit+poll) |
| Duration > 5h | Not supported, slice first then process each slice with Flash mode |
| Cannot determine duration and size ≤ 100MB | `asr_flash.py --file "<FILE>"` (Flash mode fallback) |
| Cannot determine duration and size > 100MB | `asr_standard.py --file "<FILE>"` (Standard mode fallback) |

### Public URLs

- Default: directly use `asr_standard.py --url "<URL>"`
- Do not download to local, probe, transcode, then route first
- Only when Standard mode actually fails, decide based on the error whether to enter the local download/slicing chain

When dealing with URLs, large files, or slicing decisions, read [routing_strategy.md](references/routing_strategy.md).

## Environment Variables and Authentication

Authentication uses the **new console scheme**, see: [Quick Start (New Console)](https://www.volcengine.com/docs/6561/2119699).

| Environment Variable | Purpose | Required |
|---------|------|------|
| `MODEL_SPEECH_API_KEY` | API Key (new console scheme) | **Yes** |
| `MODEL_SPEECH_APP_ID` | App ID (used with legacy authentication) | No |
| `MODEL_SPEECH_ASR_API_BASE` | Flash mode endpoint (has default) | No |
| `MODEL_SPEECH_ASR_RESOURCE_ID` | Flash mode resource ID (default `volc.bigasr.auc_turbo`) | No |
| `MODEL_SPEECH_ASR_STANDARD_SUBMIT_URL` | Standard mode submit endpoint (has default) | No |
| `MODEL_SPEECH_ASR_STANDARD_QUERY_URL` | Standard mode query endpoint (has default) | No |
| `MODEL_SPEECH_ASR_STANDARD_RESOURCE_ID` | Standard mode resource ID (default `volc.bigasr.auc`) | No |
| `FEISHU_TENANT_TOKEN` | Lark tenant_access_token (only for `--file-key` mode) | No |

## Script List

| Script | Purpose | Mode |
|------|------|----------|
| `scripts/inspect_audio.py` | Audio metadata detection (duration, sample rate, channels, etc.) | Pre-check |
| `scripts/ensure_ffmpeg.py` | Automatically detect and install ffmpeg/ffprobe | Pre-check |
| `scripts/asr_flash.py` | Flash mode recognition (≤2h/100MB, synchronous) | Express/Flash |
| `scripts/asr_standard.py` | Standard mode recognition (≤5h, async submit+poll) | Standard |

## Minimal Script Examples

```shell
# Pre-check: detect audio metadata
python3 <SKILL_DIR>/scripts/inspect_audio.py "<AUDIO_INPUT>"

# Auto-install ffmpeg if missing
python3 <SKILL_DIR>/scripts/ensure_ffmpeg.py --execute

# Flash mode (short audio, ≤2h/100MB)
python3 <SKILL_DIR>/scripts/asr_flash.py --file "<AUDIO_FILE>"

# Standard mode (long audio or URL)
python3 <SKILL_DIR>/scripts/asr_standard.py --url "<AUDIO_URL>"
python3 <SKILL_DIR>/scripts/asr_standard.py --file "<LONG_AUDIO_FILE>"

# Standard mode: submit only, no polling
python3 <SKILL_DIR>/scripts/asr_standard.py --url "<URL>" --no-poll

# Standard mode: query existing task
python3 <SKILL_DIR>/scripts/asr_standard.py --query-task-id <ID> --query-logid <LOGID>
```

## asr_flash.py (Flash Mode) Parameters

| Parameter | Required | Description |
|------|------|------|
| `--file` | Choose one of three | Local audio file path |
| `--url` | Choose one of three | Audio file URL |
| `--file-key` | Choose one of three | Lark voice message file_key |
| `--feishu-token` | No | Lark tenant_access_token |
| `--appid` | No | App ID |
| `--token` | No | API Key |
| `--language` | No | Language code |

## asr_standard.py (Standard Mode) Parameters

| Parameter | Required | Description |
|------|------|------|
| `--url` | Choose one of two | Audio file URL |
| `--file` | Choose one of two | Local audio file path |
| `--appid` | No | App ID |
| `--token` | No | API Key |
| `--language` | No | Language code |
| `--no-poll` | No | Submit task only, do not poll for results |
| `--poll-interval` | No | Polling interval in seconds (default 3) |
| `--poll-max-time` | No | Maximum polling time in seconds (default 10800) |
| `--query-task-id` | No | Query existing task ID |
| `--query-logid` | No | X-Tt-Logid passed when querying |

## Lark Voice Message Processing Flow

```
Receive audio message → Audio file downloaded to /root/.openclaw/media/inbound/ → Execute asr_flash.py --file → Return text → Process as user message
```

Common commands:

```shell
# Lark voice file (most common, file auto-downloaded by Lark plugin)
python scripts/asr_flash.py --file "/root/.openclaw/media/inbound/xxxxx.ogg"
```

## Error Handling

- `PermissionError: MODEL_SPEECH_API_KEY ...` → Prompt the user to configure the API Key
- `ASR request failed` → Check API credentials and account
- `Audio duration exceeds 5 hours` → Prompt the user to split the file
- `Audio file does not exist/is empty` → Check the file path
- **When encountering an error, directly inform the user of the specific error; do not attempt to use whisper as a substitute.**

## When to Continue Reading References

- **URL / Large files / Slicing / Routing details**: Read [routing_strategy.md](references/routing_strategy.md)

## Reference Documentation

- [Volcengine BigModel ASR](https://www.volcengine.com/docs/6561/1354870)
- [Quick Start (New Console)](https://www.volcengine.com/docs/6561/2119699) — Authentication and activation
- [API Key Usage](https://www.volcengine.com/docs/6561/1816214)
