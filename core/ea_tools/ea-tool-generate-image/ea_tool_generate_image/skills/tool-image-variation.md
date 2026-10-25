---
name: tool-image-variation
description: "Generate alternative versions of an existing image, keeping the subject and varying the details; no prompt."
hidden: true
context: inline
---

# tool-image-variation

Takes a source image and produces related images that preserve the broad subject while changing details. There is no prompt: the variation is derived from the input alone, which makes it the tool for "show me other takes on this", not "make it look like X".

## When to Use

- Explore alternative interpretations of a good composition without rewriting a prompt.
- Produce a small set of options for the user to choose from.
- Test how much of a subject survives when the model is allowed to improvise.

## When NOT to Use

- Prompt-driven generation — `generate_image`.
- Targeted regional edits (inpainting) — `edit_image`, which takes a prompt and a mask.
- Deterministic transformations (resize, rotate, convert, crop) — `image`.
- Anything requiring the output to match the source exactly; variations are by definition different.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `image` | string | yes | Source image as a base64 string or a URL. |
| `n` | integer | no | Number of variations to generate (default 1). |
| `size` | string | no | Output dimensions as `WxH` (default `1024x1024`). |

### Parameter Notes

- A square PNG source gives the most predictable compatibility; unusual aspect ratios may be cropped or rejected.
- `n` multiplies latency and cost; two or three options is usually enough to choose from.
- `size` must be supported by the backend; an unsupported value fails the call rather than silently resizing.
- Provider support varies — the operation is `create_image_variation`, and not every backend implements it.
- Outputs are independent samples: there is no guarantee of continuity between them.

## Example

```json
{
  "image": "https://example.com/assets/logo-concept.png",
  "n": 3,
  "size": "1024x1024"
}
```

## Best Practices

- Start from a clean, square, reasonably high-resolution source.
- Ask for 2–3 variations, inspect them, then pick one as the base for a follow-up edit.
- Expect structural drift: check that the subject survived and only details changed.
- Use variations to explore, then `edit_image` to converge on the chosen result.
- Keep the source path or base64 handy; variations do not carry a reference to it.

## Common Pitfalls

- Expecting the same subject with identical details; variations alter details by design.
- Passing a non-square or very small image and getting a cropped, muddy result.
- Using it when you actually wanted a specific change (use `edit_image`).
- Assuming every backend supports variations — some reject the call outright.
- Generating ten variations and paying for context with inline images.

## Anti-patterns

- Using variations as a substitute for writing a better prompt.
- Generating variations of a text-heavy image; lettering degrades.
- Blind bulk generation without reviewing the first result.

## Pairing with Other Tools

- `generate_image` — produce the initial composition.
- `edit_image` — refine the chosen variation in a specific region.
- `image` — inspect and normalise the source before varying.
- `question` — let the user pick among the generated options.
- `document` / `presentation` — place the selected asset.
