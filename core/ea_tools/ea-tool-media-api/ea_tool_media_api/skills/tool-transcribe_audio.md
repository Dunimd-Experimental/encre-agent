---
name: tool-transcribe-audio
description: "Transcribe speech from an audio file into text in the spoken language, using the active backend's Whisper-compatible model."
hidden: true
context: inline
---

# tool-transcribe_audio

`transcribe_audio` sends an audio file to the active backend's speech-to-text model and returns the transcript in the **original language**. It is the right choice for voice notes, interviews, meetings and recordings. Unlike the local Whisper path inside `media`, this runs on the configured model provider, so it needs a backend that implements transcription.

## When to Use

- Turn a voice memo, meeting recording or interview into text.
- Get a transcript you will then summarize, translate with `translation`, or index.
- Non-English audio where you want the source-language text preserved.
- Audio already in a common format (mp3, wav, m4a) and reasonably short.

## When NOT to Use

- You want English output from non-English audio in one step — use `translate_audio`.
- Translating **written** text — use `translation`.
- Long-form audio where a dedicated ASR service with diarization is available.
- Extracting metadata, trimming or converting media — use `media`.
- Reading text embedded in an image — use `image` (`ocr`).

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `file` | string | yes | Local audio file path **or** base64-encoded audio data. |
| `language` | string | no | ISO language code (`en`, `zh`, `fr`, ...) to guide transcription; omit for auto-detection. |

### Parameter Notes

- `file` accepts either a path or a base64 blob; prefer a path so the payload stays small.
- `language` is a hint, not a filter: a mismatch degrades accuracy rather than failing.
- The model returns plain text; timestamps, speaker labels and confidence scores are not exposed.

## Example

```json
{
  "file": "D:/recordings/standup.m4a",
  "language": "en"
}
```

## Best Practices

- State `language` whenever you know it — auto-detection on short or noisy clips often guesses wrong.
- Keep clips short; for long recordings, split with `media` (`trim`) and transcribe in chunks.
- Feed clean, single-speaker audio where possible; background music hurts accuracy badly.
- Pass a filesystem path rather than base64 unless the bytes exist only in memory.

## Common Pitfalls

- Backends without transcription support return an "unsupported" error — check the provider before relying on this.
- Very long files may exceed the model's duration or size limit; there is no built-in chunking.
- Multiple speakers produce one undifferentiated block of text.
- Heavy accents, crosstalk and domain jargon produce plausible-looking but wrong words; verify names and numbers.

## Anti-patterns

- Using this to translate (that is `translate_audio`).
- Transcribing an hour of audio in one call and hoping for segmentation.
- Treating the transcript as verbatim truth for legal or medical use.

## Pairing with Other Tools

- `media` — trim or extract the audio track before transcribing.
- `translate_audio` — when English output is wanted directly.
- `translation` — translate the resulting text into another language.
- `document` — save the transcript as a shareable document.
