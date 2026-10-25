---
name: byted-text-to-speech
description: Convert text to speech with Volcengine Doubao voice synthesis (HTTP chunked/SSE unidirectional V3) — pick a voice, tune speed/pitch/volume, choose mp3/pcm/ogg_opus, filter Markdown, and read LaTeX. Use for narration, dubbing, audiobook clips, or any read-aloud request.
---

# Doubao Text-to-Speech

Turn a block of text into a playable audio file through Volcengine's Doubao speech synthesis API (V3 HTTP unidirectional streaming). The bundled script handles authentication, streaming assembly, and file output, and returns a JSON result with the local path.

## When to Use

- "Read this out loud", "turn this article into audio", "generate narration/dubbing".
- Producing an audiobook clip, podcast intro, IVR prompt, or video voice-over.
- Synthesizing multilingual text (Chinese, English, Japanese, Spanish, and dialect voices are available).
- Reading code comments, docs, or reports aloud for review.

## When NOT to Use

- Speech-to-text or captioning → use `transcribe_audio` (or `byted-voice-to-text` for the Doubao ASR service).
- Translating audio between languages → `translate_audio`.
- Generating music or sound effects → `byted-music-generate`.
- Cloning a voice → this skill uses stock voices only; voice cloning is a separate console workflow.
- Simple local format conversion of an existing audio file → `media`.

## Prerequisites

- `MODEL_SPEECH_API_KEY` in the environment. Get it from the Volcengine speech console: <https://console.volcengine.com/speech/new/setting/apikeys>.
- The Doubao speech service activated on that account.
- If the key is missing, read `references/setup-guide.md` and walk the user through activation instead of fabricating output.

## Script Parameters

| Parameter | Short | Required | Description |
|-----------|-------|----------|-------------|
| `--text` | `-t` | Yes | Text to synthesize. |
| `--output` | `-o` | No | Output audio path; auto-generated when omitted. |
| `--speaker` | `-s` | No | Voice id, default `zh_female_vv_uranus_bigtts`. Voice list: <https://www.volcengine.com/docs/6561/1257544>. |
| `--format` | | No | `mp3` (default), `pcm`, `ogg_opus`. The API also accepts `wav`, but streaming responses repeat WAV headers, so `pcm` is the correct choice for streaming. |
| `--sample-rate` | | No | 8000 / 16000 / 22050 / 24000 / 32000 / 44100 / 48000; default 24000. `ogg_opus` only supports 48000. |
| `--speech-rate` | | No | Speed in [-50, 100]; 100 = 2.0x, -50 = 0.5x, default 0. |
| `--pitch-rate` | | No | Pitch in [-12, 12], default 0. |
| `--loudness-rate` | | No | Volume in [-50, 100]; 100 = 2.0x, -50 = 0.5x, default 0. |
| `--bit-rate` | | No | Bitrate for `mp3` / `ogg_opus` (64000–160000; default 64000). Ignored for `pcm` and `wav`. |
| `--filter-markdown` | | No | Strip Markdown syntax before synthesis (`**Hello**` reads as "Hello"). Off by default. |
| `--enable-latex` | | No | Read LaTeX formulas (latex_parser v2); implies Markdown filtering. Off by default. |

## Workflow

1. Verify `MODEL_SPEECH_API_KEY` is present (`terminal`: `echo $env:MODEL_SPEECH_API_KEY` on Windows, `printenv` elsewhere). Never echo the value itself.
2. Decide the target: voice, format, and whether the text contains Markdown or math.
3. Run `scripts/text_to_speech.py` with the flags above, writing to a deterministic `--output` path.
4. Parse the JSON result: `status`, `local_path`, `format`, and `error` on failure.
5. Report the path (and duration/size) so the user can play or download it.

## Example

```shell
# Basic synthesis to a chosen path
python scripts/text_to_speech.py \
  -t "Welcome to the Encre Agent voice synthesis demo." \
  -o ./out/welcome.mp3

# A different voice, 16 kHz, slightly faster, Markdown stripped
python scripts/text_to_speech.py \
  -t "**Note:** this paragraph came from a README." \
  -s zh_female_vv_uranus_bigtts \
  --sample-rate 16000 --speech-rate 10 --filter-markdown \
  -o ./out/note.mp3

# Read a document containing formulas
python scripts/text_to_speech.py -t "$(cat chapter.md)" --enable-latex -o ./out/chapter.mp3
```

## Return Value

The script prints JSON: `{"status": "success"|"error", "local_path": "...", "format": "...", "error": "..."}`. Treat anything other than `status == "success"` as a failure and surface `error` verbatim.

## Best Practices

- Set `--bit-rate 160000` for music-adjacent or high-fidelity narration; keep 64000 for speech-only drafts.
- Use `pcm` when piping into another tool (muxing, ASR) and `mp3` for delivery.
- Prefer 24000 Hz unless a downstream pipeline demands otherwise.
- Keep `--speech-rate` within ±20 for natural pacing; extreme values sound robotic.
- Strip Markdown for prose documents and keep it when the structure should be spoken (headings read naturally).

## Common Pitfalls

- `PermissionError: MODEL_SPEECH_API_KEY ... needs to be configured`: the key is absent from the environment; configure it and retry rather than switching voices.
- Requesting `ogg_opus` with a sample rate other than 48000.
- Expecting `--bit-rate` to affect `pcm`/`wav` output — it does not.
- Very long single calls time out or truncate; split long documents into paragraphs and concatenate the resulting files with `media`.
- A 4xx/5xx usually means an invalid speaker id, an unactivated service, or an empty text field.

## Anti-patterns

- Passing HTML or raw Markdown without `--filter-markdown`, then blaming the voice for reading asterisks.
- Re-running synthesis for every small edit instead of regenerating only the changed paragraph.
- Hard-coding the API key into scripts or logs.

## Sources

- V3 HTTP chunked/SSE unidirectional synthesis (formats, sample rates, rate ranges): <https://www.volcengine.com/docs/6561/1598757>
- Unidirectional streaming HTTP reference: <https://docs.volcengine.com/docs/6561/2528925>
- Voice list: <https://www.volcengine.com/docs/6561/1257544>

## References

- `references/setup-guide.md` — service activation, key creation, environment configuration.
- `references/docs-index.md` — API parameter index, voice list, error-code quick reference.
