---
name: tool-translate-audio
description: "Translate non-English speech directly into English text from an audio file, using the active backend's translation model."
hidden: true
context: inline
---

# tool-translate_audio

`translate_audio` converts speech in any supported language into **English text** in a single step: transcription and translation happen in the model, with no intermediate source-language text returned. Choose it when only the English meaning matters.

## When to Use

- A non-English recording should be understood in English, and the original wording is not needed.
- You want one call instead of transcribe-then-translate.
- Building English notes from foreign-language calls, meetings or voice notes.
- The audio is short and in a format the backend accepts.

## When NOT to Use

- You need the original-language transcript — use `transcribe_audio`.
- The source is already English — transcription, not translation.
- Translating written text or documents — use `translation`.
- You need both the source text and the translation (run `transcribe_audio` then `translation`).
- Media conversion, trimming or metadata — use `media`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `file` | string | yes | Local audio file path or base64-encoded audio data. |

### Parameter Notes

- There is no `language` parameter: the source language is auto-detected by the model.
- There is no target-language parameter: output is always English.
- Output is plain English text — no timestamps, no speaker attribution, no original-language echo.

## Example

```json
{
  "file": "D:/recordings/interview-zh.mp3"
}
```

## Best Practices

- Prefer clean, single-speaker audio; translation quality tracks transcription quality.
- For anything where the original wording matters, transcribe first and translate the text with `translation` so both versions exist.
- Split long recordings with `media` before calling, to stay inside model limits.
- Re-check names, numbers and technical terms — translation tends to smooth them over.

## Common Pitfalls

- The backend must implement `translate_audio`; otherwise the call returns an unsupported error.
- Auto-detection on short clips can pick the wrong source language and produce nonsense.
- Nuance, idiom and hedging are lost; do not treat the English as an exact rendering.
- Long files may exceed duration/size limits — there is no chunking here.

## Anti-patterns

- Using this for English audio "just in case" (it is transcription, not translation).
- Using it to translate documents or chat text — `translation` is the right tool.
- Expecting the source-language transcript back; it is discarded by design.

## Pairing with Other Tools

- `transcribe_audio` — get the original-language transcript first when fidelity matters.
- `translation` — translate written text or the resulting transcript into other languages.
- `media` — trim, convert or extract the audio track beforehand.
- `document` — store the English text as a deliverable.
