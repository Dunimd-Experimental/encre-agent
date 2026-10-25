---
name: byted-mediakit-image
description: Run Volcengine AI MediaKit image tools via mediakit-cli — OCR, erase & inpaint (icons, text, regions), background removal with optional contour or crop, quality enhancement/upscaling, and aesthetic/quality scoring. All tools are cloud-only and synchronous.
---

# MediaKit Image

Five cloud image capabilities exposed by `mediakit-cli image <tool> [flags]`: read text out of an image, erase unwanted content, cut the subject out, upscale and restore quality, or score how good an image looks. Every tool in this domain is **synchronous and cloud only** — the final result comes back in the same call, so no `query-task` polling is needed.

## When to Use

- Extracting printed text with boxes and confidence from screenshots, scans, or photos (`image-ocr`).
- Removing icons, watermarks, burned-in captions, or a described region and refilling the background (`erase-image`).
- Producing a transparent PNG/WebP of a person or product, optionally with a contour or tightly cropped to the subject (`remove-image-background`).
- Upscaling a low-resolution image or fixing clarity/color before publishing (`enhance-image`).
- Scoring image quality or aesthetics to pick the best frame from a batch (`evaluate-image-quality`).

## When NOT to Use

- Cropping, resizing, rotating, compressing, converting formats, or face blur locally → use the `image` tool (local Pillow/ffmpeg path, no API key).
- Generating a brand-new image from a prompt → use `generate_image`; editing an existing one with a prompt → `edit_image`.
- Reading text from a PDF or a multi-page document → use `pdf` first to render or extract, then OCR the images.
- Offline work with no `MEDIAKIT_API_KEY` → these tools cannot run locally; say so instead of substituting silently.

## Prerequisites

- `mediakit-cli` installed (`npx @volcengine/mediakit-cli install -y`) and initialised with an API key (`mediakit-cli init --api-key <key> --credential-store config --yes`).
- `MEDIAKIT_API_KEY` available; set `MEDIAKIT_SURFACE=skill` on every invocation made by this skill.
- Inputs must be publicly reachable URLs in `png/jpg/jpeg/webp/tiff/bmp/ico`, ≤ 10 MB per image (cloud constraint).
- Read `reference/shared.md` for mode rules, idempotency, and error shapes.

## Tools in this skill

| Tool | What it does | Key parameters |
|------|--------------|----------------|
| `image-ocr` | Printed-text OCR returning editable text, box coordinates, and confidence. | `image_url`, `callback_args?`, `client_token?` |
| `erase-image` | Detects and erases common icons/text or a specified region, then inpaints the background. | `image_url`, `tool_version?`, `standard_scene?`, `standard_erase_text?`, `output_format?`, `callback_args?`, `client_token?` |
| `remove-image-background` | Keeps the main subject and outputs a transparent-background image. | `image_url`, `scene` (`general`\|`human`\|`product`), `need_contour?`, `contour_color?`, `contour_size?`, `need_crop_background?`, `output_format?` (`png`\|`jpeg`\|`webp`) |
| `enhance-image` | Content-aware enhancement of resolution, clarity, and color. | `image_url`, `tool_version?`, `multiple?`, `target_width?`, `target_height?` |
| `evaluate-image-quality` | Subjective + objective quality and aesthetic scoring. | `image_url`, `tool_version?`, `standard_evaluate_items?` (array of item names) |

All five accept `callback_args` and `client_token` (≤ 64 chars, reused for retries of the same logical request).

## Workflow

1. Read `reference/shared.md`; confirm credentials with `mediakit-cli config show`.
2. Confirm the parameter surface for the installed version: `mediakit-cli image <tool> --schema`.
3. Call the tool directly (no polling). Parse the returned `image_url` / text payload.
4. For chained work, feed the returned URL straight into the next tool, or download it to disk with `terminal` before further local processing.
5. Report the output URL plus the fields that matter (format, width/height, size, scores).

## Example

```shell
# OCR a screenshot
MEDIAKIT_SURFACE=skill mediakit-cli image image-ocr \
  --image-url https://example.com/shot.png

# Cut out a person with a white 10px contour, cropped to the subject
MEDIAKIT_SURFACE=skill mediakit-cli image remove-image-background \
  --image-url https://example.com/person.jpg \
  --scene human --need-contour true --contour-color "#FFFFFF" \
  --contour-size 10 --need-crop-background true --output-format png

# Erase burned-in subtitles, then enhance the result
MEDIAKIT_SURFACE=skill mediakit-cli image erase-image \
  --image-url https://example.com/frame.jpg --output-format png

MEDIAKIT_SURFACE=skill mediakit-cli image enhance-image \
  --image-url <returned_image_url> --multiple 2

# Score candidates before choosing one
mediakit-cli image evaluate-image-quality \
  --image-url https://example.com/a.jpg \
  --standard-evaluate-items '["quality","aesthetic"]'
```

## Best Practices

- Use `scene general` when the subject class is unknown; `human`/`product` unlock contour and background-crop options.
- Request `output_format png` whenever transparency must survive; `jpeg` flattens the alpha channel.
- Prefer `target_width`/`target_height` over `multiple` when a downstream layout needs exact dimensions.
- For batches, keep one `client_token` per input image so a retry does not produce a second artifact.
- Download results to a local directory before reporting; cloud URLs are temporary.

## Common Pitfalls

- Sending a local path where the API requires a public URL — cloud image tools do not upload local files for you.
- Exceeding 10 MB or using an unsupported container (`gif`, `heic`) and reading the raw error as an empty result.
- Setting `contour_*` or `need_crop_background` with `scene general` — those options are ignored.
- Treating `evaluate-image-quality` output as a hard pass/fail gate; the scores are comparative signals, not guarantees.
- Running `shared query-task` after these calls — nothing is queued, results are immediate.

## Anti-patterns

- Looping the same image through `enhance-image` repeatedly hoping for cumulative gains.
- Using OCR output verbatim without checking low-confidence boxes on noisy scans.
- Erasing a region without confirming what was removed; always show the before/after result path.
- Inventing `tool_version` values — omit the parameter unless the official docs for your account name a specific version.

## Sources

- AI MediaKit capability catalogue (image tools: background removal, erase & inpaint, OCR): <https://www.volcengine.com/docs/6448/2222230>
- AI MediaKit Skill user guide: <https://docs.volcengine.com/docs/6448/2298695>
- mediakit-cli repository: <https://github.com/volcengine/mediakit-cli>

## References

- `reference/shared.md` — prerequisites, auth, modes, idempotency, error shapes.
- `reference/image-ocr.md`, `reference/erase-image.md`, `reference/remove-image-background.md`, `reference/enhance-image.md`, `reference/evaluate-image-quality.md` — per-tool parameters and response fields.
