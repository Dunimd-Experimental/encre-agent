---
name: tool-generate-image
description: "Generate original images from a text prompt with the backend's image model; returns one or more rendered images."
hidden: true
context: inline
---

# tool-generate-image

Produces new images from a text description using the active backend's image-generation model. It is a generative model, not a renderer: output is not deterministic and the same prompt can yield different compositions.

## When to Use

- Original artwork, illustrations, icons, or decorative visuals for a document or slide.
- Photorealistic or painterly imagery that no diagram/chart tool can express.
- Concept exploration: several variations of a described scene.
- Placeholder assets for a UI mockup.

## When NOT to Use

- Editing or transforming an existing image — use `edit_image`.
- Diagrams, flowcharts, or architecture — use `diagram`.
- Data visualisation — use `chart`.
- Text-heavy images (posters, logos with precise lettering); generation models still garble text.
- Anything that must be exact, reproducible, or legally cleared — generated assets need review.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | yes | Description of the image: subject, style, composition, lighting, mood. |
| `n` | integer | no | Number of images to generate (default 1). |
| `size` | string | no | Dimensions as `WxH`, e.g. `1024x1024` (default). Valid values depend on the backend. |

### Parameter Notes

- The backend must implement `generate_image`; otherwise the call returns an unsupported error rather than degrading.
- Supported `size` values are backend-specific — common options are `1024x1024`, `1024x1792`, `1792x1024`. An unsupported size fails the call.
- `n > 1` multiplies cost and latency, and each result consumes context if returned inline. Prefer separate calls when you intend to iterate.
- Prompts should be concrete about style and framing; abstract prompts get generic results.
- Generated images carry no provenance metadata you can rely on for attribution.

## Example

```json
{
  "prompt": "A clean isometric illustration of a modular agent runtime: a central glowing core connected to five small tool modules, dark navy background, soft rim lighting, minimal geometric style, no text",
  "n": 1,
  "size": "1024x1024"
}
```

Wide hero image:

```json
{
  "prompt": "Wide minimalist banner showing a desktop app window floating above a circuit-board landscape, teal and graphite palette, volumetric light, no text",
  "size": "1792x1024"
}
```

## Best Practices

- Specify the four levers explicitly: subject, style, composition/camera, and palette. Vague prompts waste a generation.
- Say "no text" when you do not want lettering; models add pseudo-text otherwise.
- Match `size` to the destination: square for icons/avatars, wide for banners and slides.
- Generate one image, inspect it, then iterate on the prompt rather than firing `n: 4` blind.
- Describe what should be in frame **and** what should not ("empty background", "no people").
- For a series, reuse the same style sentence and vary only the subject.

## Common Pitfalls

- Asking for legible text, charts, or precise logos — the model will approximate and mangle them.
- Using an unsupported `size` for the configured backend.
- Assuming the backend supports image generation at all; some do not.
- Iterating by adding more adjectives instead of changing composition or style.
- Expecting consistency across calls (same character, same product) — there is no seed control exposed.
- Ignoring content-policy refusals and retrying the same prompt.

## Anti-patterns

- Using generation for something `diagram` or `chart` renders exactly.
- Regenerating an image repeatedly hoping for "the same but better" without editing the prompt.
- Embedding generated images in deliverables without a human review pass.
- Requesting a photorealistic depiction of a real, identifiable person.

## Pairing with Other Tools

- `edit_image` — modify or extend a generated result.
- `image` — inspect, resize, or convert the output.
- `image_variation` — explore alternatives from an existing image.
- `document` / `presentation` — place the final asset.
- `diagram` / `chart` — when precision matters more than aesthetics.
