---
name: tool-edit-image
description: "Inpaint an existing image from a text prompt, optionally restricted to the region revealed by a transparent mask."
hidden: true
context: inline
---

# tool-edit-image

Modifies a supplied image according to a text instruction. With a mask, only the transparent region is repainted and the rest is preserved; without one, the model edits the image as a whole while trying to keep it recognisable. This is the tool for "change this part", not "make something new".

## When to Use

- Remove or replace an object in a photo while keeping the surroundings intact.
- Repaint a region (sky, background, a label) without regenerating the whole frame.
- Touch up a generated asset: fix a hand, a texture, a colour cast.
- Iterate on a specific area after `generate_image` produced a good composition.

## When NOT to Use

- Creating an image from nothing — `generate_image`.
- Exploring different overall interpretations of the same subject — `image_variation`.
- Programmatic image processing (crop, resize, format conversion, colour space) — use `image`; a diffusion edit would alter content.
- Precise text replacement or layout work — models still garble lettering.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `image` | string | yes | Source image, as a base64 string or a URL. |
| `prompt` | string | yes | Description of the desired edit. |
| `mask` | string | no | Mask image (base64 or URL) where fully transparent pixels mark the editable region. |

### Parameter Notes

- The mask convention is inverted relative to some editors: **transparent ⇒ editable**, opaque ⇒ preserved. Getting this backwards edits everything except what you meant.
- Without `mask`, the model may still change global tone or composition; supply a mask when preservation matters.
- `image` and `mask` must be mutually compatible in size for predictable results.
- Describe the target state ("a red ceramic mug on the table"), not the operation ("remove the blue mug").
- The backend must implement `edit_image` and accept the encoding you send; otherwise the call fails as unsupported.

## Example

Masked object replacement:

```json
{
  "image": "https://example.com/assets/desk-photo.png",
  "mask": "https://example.com/assets/desk-mask.png",
  "prompt": "A matte black mechanical keyboard with warm white backlighting, same perspective and lighting as the rest of the photo"
}
```

Unmasked global restyle:

```json
{
  "image": "<base64-png>",
  "prompt": "Same scene, late-afternoon golden light, softer contrast, film grain"
}
```

## Best Practices

- Always mask when the surrounding content must survive; unmasked edits drift.
- Keep the mask tight around the object plus a few pixels of feathering; huge masks reduce fidelity elsewhere.
- State materials, lighting, and perspective in the prompt so the patch matches its surroundings.
- Change one thing per call. Iterating on a single variable keeps the result attributable.
- Compare the result to the source before accepting it — check edges, shadows, and repeated textures.
- Save/keep the source image; edits are not reversible from the output alone.

## Common Pitfalls

- Inverting the mask (transparent = keep) and repainting the background instead of the subject.
- Describing the removal instead of the replacement.
- Expecting exact colour matching or typography to be reproduced.
- Feeding a JPEG URL where the backend expects PNG base64 (or vice versa).
- Editing repeatedly: each pass degrades detail and accumulates artefacts.
- Assuming the backend supports edits at all.

## Anti-patterns

- Using `edit_image` for a simple crop or rotation.
- Re-editing the same region five times instead of regenerating with a better prompt.
- Editing a face or a document scan to change its meaning.
- Using it to "fix" a diagram whose labels came out wrong — use `diagram`.

## Pairing with Other Tools

- `generate_image` — create the base image first.
- `image_variation` — explore alternatives instead of targeted edits.
- `image` — crop/resize/convert before and after editing.
- `computer` — when the image only exists on screen and must be captured first.
- `document` / `presentation` — place the final edited asset.
