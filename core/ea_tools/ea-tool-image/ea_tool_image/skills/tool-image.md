---
name: tool-image
description: "Inspect image metadata/EXIF, OCR printed text out of an image, or re-encode to another format using Pillow and pytesseract."
hidden: true
context: inline
---

# tool-image

`image` handles the three read-side image jobs: reading a file's format, dimensions and EXIF as JSON (`info`), extracting printed text with Tesseract (`ocr`), and re-encoding to a different format (`convert`). It does not create or creatively edit images — that is `generate_image` and friends.

## When to Use

- Check format, size and EXIF before attaching or processing an image: `action="info"`.
- Pull text out of a screenshot, scan or photo: `action="ocr"`.
- Normalize a file to PNG/JPEG/WEBP before upload or embedding: `action="convert"`.
- Confirm a downloaded or generated image is valid and what it claims to be.
- Read orientation metadata when a photo appears rotated downstream.

## When NOT to Use

- Generating an image from a prompt — use `generate_image`.
- Editing pixels, filters, crops or upscaling — no such action here; use an external pipeline.
- Rendering PDF pages to images — use `pdf`.
- Reading a textual description of a photo by a vision model — pass the image to a model that accepts images, not OCR.
- Video frames or audio — use `media`.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `info` (format/size/EXIF JSON), `ocr` (text extraction), `convert` (re-encode). |
| `file_path` | string | yes | Path to the source image (PNG, JPEG, WEBP, ...). |
| `options` | object | no | Convert options: `format` (target format, default `PNG`), `output` (output path, default `<base>_converted.<ext>`). |

### Parameter Notes

- `options` is only read by `convert`; `format` is a Pillow format string (`PNG`, `JPEG`, `WEBP`), not a MIME type.
- Without `options.output`, the converted file lands next to the source with a `_converted` suffix — always state the path explicitly if a later step consumes it.
- `action`, `file_path`, `options` are the only accepted keys; OCR language and page-segmentation settings are not exposed.

## Example

```json
{
  "action": "convert",
  "file_path": "D:/assets/screenshot.jpg",
  "options": { "format": "PNG", "output": "D:/assets/screenshot.png" }
}
```

## Best Practices

- Run `info` first when the format is unknown; it prevents a failed `convert` on a mislabeled extension.
- For OCR, prefer high-resolution, axis-aligned crops; upscale small text and crop to the text region beforehand if quality is poor.
- Convert to PNG for lossless intermediate steps, JPEG only for final size-sensitive delivery.
- Keep `file_path` absolute so the default `_converted` output does not land somewhere unexpected.

## Common Pitfalls

- OCR needs the `pytesseract` Python package **and** a system Tesseract binary; without them the tool returns an install hint instead of text.
- OCR on stylized fonts, handwriting, low DPI or rotated text produces confident garbage — spot-check the output.
- Converting JPEG → PNG inflates size with no quality gain.
- EXIF may be absent entirely; do not treat an empty map as an error.
- Animated formats collapse to the first frame on convert.

## Anti-patterns

- Using OCR to read a chart, diagram or table layout — it returns loose text, not structure.
- Converting in a loop to "try formats" instead of checking `info` once.
- Using this tool to resize/crop; it only re-encodes.

## Pairing with Other Tools

- `generate_image` / `edit_image` / `image_variation` — produce or modify images, then verify with `image`.
- `pdf` — render or read PDFs whose pages you then OCR.
- `media` — video frame extraction, then OCR on the frames.
- `file_read` — inspect small text sidecars; not for binary images.
