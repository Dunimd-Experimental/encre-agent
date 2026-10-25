
## Quick Reference

**Formats:** landscape `1920x1080` by default; portrait `1080x1920`; square `1080x1080`. Set the canvas once in the storyboard frontmatter (`canvas: { w, h, fps }`).

**Scripts** under `scripts/`: `analyze-beatgrid.py` (the one analyzer), `validate-plan.mjs` (plan check), `assemble-index.mjs` (index assembly), `stage-assets.mjs` (stage user media), `lib/storyboard.mjs` (vendored parser). Everything else is the `hyperframes` CLI.

| Read                                                                                                           | When                                                    |
| -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| [`references/frame-skeleton.md`](references/frame-skeleton.md)                                                 | Step 2: read the music, lay out the frames, set pacing  |
| [`references/planning.md`](references/planning.md) · [`storyboard-format.md`](references/storyboard-format.md) | Step 3: pick the brand, fill each frame, write the plan |
| [`references/template-catalog.md`](references/template-catalog.md)                                             | Step 3: pick a template per group                       |
| [`references/motion-primitive-catalog.md`](references/motion-primitive-catalog.md)                             | Step 3/4: L0 recipes for free-compose                   |
| [`references/montage.md`](references/montage.md)                                                               | Step 3/4: asset treatments (beat-cut / ken-burns)       |
| [`sub-agents/frame-worker.md`](sub-agents/frame-worker.md)                                                     | Step 4: dispatch + build one frame                      |
| `../hyperframes-core/references/subagent-dispatch.md`                                                          | Step 4: dispatch sub-agents safely                      |
| `../hyperframes-creative/references/design-spec.md`                                                            | Step 3: pick the preset (the brand)                     |

## Directory layout

```
music-to-video/
  SKILL.md
  references/   frame-skeleton.md · planning.md · storyboard-format.md
                template-catalog.md · motion-primitive-catalog.md · montage.md
                templates/<id>/          { index.html (+ assets/ · program.json) }  ← L1 catalog impls
                motion-primitives/<id>/  { index.html } (+ ../assets/gsap.min.js shared by recipes) ← L0 catalog impls
  scripts/      analyze-beatgrid.py · assemble-index.mjs · validate-plan.mjs · stage-assets.mjs · lib/storyboard.mjs
  sub-agents/   frame-worker.md   ← the one subagent (one per frame)
```
