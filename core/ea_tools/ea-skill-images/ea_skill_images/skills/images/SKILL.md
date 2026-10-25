---
name: images
description: Image file processing - inspect metadata, OCR text in images, convert formats, and visually understand image content
aliases: [image-processing, picture]
when_to_use: ".png .jpg .jpeg .gif .bmp .webp .tiff .svg"
argument_hint: "[path to image file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## Image File Processing

You are processing an image file: **{{args}}**

### When to Use
- Inspect an image's metadata (dimensions, format, size) before deciding how to handle it
- OCR (extract text) from a screenshot, photo, or scanned image
- Convert an image to another format (png <-> jpg <-> webp)
- Visually understand the content of an image (what is depicted, read a chart/diagram)

### When NOT to Use
- **A PDF that happens to contain images** -> use the `pdf` skill; render pages then OCR if needed
- **Edit pixels / composite / draw on an image** -> the tools here are read/ocr/convert; for pixel editing use `terminal` with Pillow/OpenCV
- **A video frame** -> extract the frame first (see `video` skill), then treat it as an image

### Processing Workflow
1. **Inspect first** -> call `image` with `action: info` to get dimensions, format, and size. Large images may need resizing before visual read.
2. **Choose the operation:**
   - Need the text in the image? -> `action: ocr`
   - Need to convert format? -> `action: convert`
   - Need to understand the content visually? -> `file_read` with `as_image` (the model sees the image directly)
   - Need metadata only? -> `action: info`
3. **Visual understanding** -> for charts, diagrams, screenshots with UI, prefer `file_read` `as_image` so the model interprets the image directly rather than relying on OCR alone.
4. **OCR vs visual** -> OCR is best for dense text in images; visual read is best for understanding layout, charts, or scenes. Use both when needed.
5. **Large images** -> resize or convert to a smaller format before visual read to control cost.

### Tool Selection
- `image` tool (registered): `action: info` / `ocr` / `convert` - primary path
- `file_read` with `as_image`: visual understanding - the model sees the image content directly
- `terminal` + Pillow/OpenCV: pixel-level edits, resizing, compositing, batch processing
- `pdf` skill: when the image is actually a page inside a PDF

### Best Practices
- `info` before `ocr`/`visual` - knowing dimensions and format shapes your approach
- For screenshots of UI/code, visual read (`file_read as_image`) usually beats OCR
- For photos of dense printed text, OCR usually beats visual description
- Keep the original; write conversions to a new path, do not overwrite unless asked
- When converting for size, prefer webp/png for screenshots, jpg for photos

### Common Pitfalls
- **OCRing an image that needs visual understanding** -> OCR returns text but misses layout, charts, and spatial relationships; use `file_read as_image` for those
- **Visually reading a huge image without resizing** -> wastes budget; `info` first, resize if large
- **Overwriting the original on convert** -> write to a new path; keep the source
- **Ignoring format constraints** -> some targets (e.g. transparency) require png/webp, not jpg

### Pairing with Other Tools
- `file_read` (as_image) - visual understanding
- `pdf` - when the image is a PDF page
- `terminal` (Pillow/OpenCV) - resize, crop, composite, batch
- `file_write` - save OCR text to a file
- `video` - extract a frame from a video, then process as an image
