---
name: tool-translation
description: "Translate text or files, detect a language, or list supported codes via Google, MyMemory or a self-hosted LibreTranslate backend."
hidden: true
context: inline
---

# tool-translation

`translation` converts written text between languages, identifies a text's language, and enumerates supported codes. It is backend-agnostic: `service="auto"` tries Google (deep-translator), then MyMemory, then LibreTranslate, so a working result usually comes back even when one provider is unreachable. File translation writes a sibling file with a `_<target>` suffix.

## When to Use

- Convert a passage, message or document into another language.
- Identify an unknown text's language before translating it.
- Look up which language codes the tool accepts.
- Translate a whole text file in one call with `file_path`.
- Translate HTML while keeping tags intact with `preserve_format=true`.
- Work privately against a local LibreTranslate instance with `service="libre"`.

## When NOT to Use

- Speech in audio files — use `translate_audio` (to English) or `transcribe_audio`.
- Summarization, rewriting or tone changes — that is a model turn, not translation.
- In-app i18n string catalogs — use a proper localization pipeline.
- OCR of a scanned page — extract text first with `image` / `pdf`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `translate`, `detect`, `languages`. |
| `text` | string | no | Text to translate or detect; required unless `file_path` is given. |
| `source_lang` | string | no | Source code (e.g. `en`, `zh`, `fr`); default `auto`. |
| `target_lang` | string | no | Target code for `translate`; default `en`. |
| `file_path` | string | no | Text file to translate; output saved as `<stem>_<target_lang>.<ext>`. |
| `service` | string | no | `auto` (default), `google`, `mymemory`, `libre`. |
| `engine_url` | string | no | LibreTranslate base URL; default `http://localhost:5000`. |
| `preserve_format` | boolean | no | Treat input as HTML and preserve markup; default false. |

### Parameter Notes

- `source_lang="auto"` is fine for Google and Libre, but MyMemory needs an explicit source when detecting.
- `target_lang` defaults to English — always set it when the user wants something else.
- With `file_path`, the translated file is written next to the source; the text is not returned inline.
- `preserve_format` only matters for HTML; for markdown it can mangle syntax.

## Example

```json
{
  "action": "translate",
  "text": "The build failed because the config key was missing.",
  "source_lang": "en",
  "target_lang": "zh"
}
```

## Best Practices

- Translate in paragraph-sized chunks; long inputs may be silently split and rejoined.
- Explicitly set `source_lang` when you know it — detection on short strings is unreliable.
- Use `service="libre"` with `engine_url` when content must not leave the network.
- For files, read the produced `_<target>` file afterwards to verify.
- Keep `preserve_format=false` for plain text and markdown unless you actually need HTML fidelity.

## Common Pitfalls

- MyMemory limits requests to ~500 characters; long text is chunked, which can break sentence context.
- `service="google"` requires the `deep-translator` package to be installed.
- `service="libre"` fails if nothing is listening at `engine_url`.
- Auto-detection on very short input (a word or two) often picks the wrong language.
- Names, product terms and code identifiers get "translated" into nonsense — protect them.

## Anti-patterns

- Round-tripping through translation to paraphrase text.
- Using this for audio or image content.
- Hard-coding `service="google"` in environments where it is blocked.
- Translating code or config values along with prose.

## Pairing with Other Tools

- `transcribe_audio` — speech to source-language text, then translate the text here.
- `translate_audio` — direct speech-to-English when the source text is not needed.
- `file_read` / `file_write` — handle the translated file.
- `document` — deliver a translated document to the user.
