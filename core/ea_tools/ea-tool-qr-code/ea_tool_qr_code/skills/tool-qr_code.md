---
name: tool-qr-code
description: "Generate QR codes as PNG/JPG/BMP or SVG, or decode QR codes and barcodes from an existing image."
hidden: true
context: inline
---

# tool-qr_code

`qr_code` handles both directions of 2D codes: `generate` writes a raster image, `generate_svg` writes a scalable vector, and `read` decodes codes from an existing image. Sizing, quiet zone, colors and error-correction level are all controllable, which matters when a code will be printed small or placed on a colored background.

## When to Use

- Encode a URL, Wi-Fi payload, vCard or tracking string into an image for the user.
- Produce a print-ready SVG that will be scaled to any size.
- Decode a code from a screenshot or scanned page.
- Control contrast or quiet zone for a code that must survive a busy background.
- Raise error correction for stickers, labels or partially obscured placement.

## When NOT to Use

- Image format conversion or OCR — use `image`.
- Charts or dashboards — use `chart` / `info`.
- AI image generation — use `generate_image`.
- PDF417 or other 1D/2D symbologies not supported by pyzbar.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `generate` (raster), `generate_svg` (vector), `read` (decode). |
| `data` | string | no | Payload to encode; required for both generate actions, ignored by `read`. |
| `file_path` | string | no | Output path when generating; input image when reading (required for `read`). |
| `format` | string | no | Raster format: `png` (default), `jpg`, `bmp`. |
| `size` | integer | no | Module (box) size in pixels; default 10. |
| `border` | integer | no | Quiet-zone width in modules; default 4. |
| `fill_color` | string | no | Foreground color; default `black`. |
| `back_color` | string | no | Background color; default `white`. |
| `error_correction` | string | no | `L` (7%), `M` (15%, default), `Q` (25%), `H` (30%). |

### Parameter Notes

- `size` is per-module pixels, not the final image size: a 25-module code at `size=10` is roughly 250px plus border.
- `border` of 4 modules is the spec minimum — shrinking it breaks many scanners.
- `fill_color` / `back_color` must keep strong contrast; dark-on-light only for reliable decoding.
- `error_correction` trades payload capacity for damage tolerance; `H` fits less data.
- `format` is ignored by `generate_svg`.

## Example

```json
{
  "action": "generate_svg",
  "data": "https://encre.example.com/invite/7f3a",
  "file_path": "D:/out/invite.svg",
  "error_correction": "Q"
}
```

## Best Practices

- Use `generate_svg` for anything printed or resized; raster codes blur when scaled.
- Raise `error_correction` to `Q` or `H` for labels that may be scuffed or partially covered.
- Keep `border` at 4 or higher so scanners can find the quiet zone.
- Verify the payload is short — long URLs increase module density and shrink effective dot size.
- After generating, decode it once with `read` when correctness is critical.

## Common Pitfalls

- Decoding needs `pyzbar` **plus** the system zbar library; otherwise `read` returns an install hint.
- Low contrast (gray on white, colored backgrounds) fails at scan time even though the image looks fine.
- A tiny `size` with a long payload produces a dense grid that phone cameras cannot resolve.
- `read` on a photo needs the code to be reasonably axis-aligned and in focus.
- Overwriting `file_path` silently replaces an existing image.

## Anti-patterns

- Encoding secrets or credentials into a QR code you then post in chat.
- Generating a raster code for print at a large physical size.
- Using `read` to extract text from a document (that is OCR).
- Lowering `border` to zero to "save space".

## Pairing with Other Tools

- `image` — inspect or convert the generated file.
- `info` — show the code inside a card or gallery.
- `file_write` — save payload text alongside the code.
- `web_fetch` / `rest_client` — validate the encoded URL resolves.
