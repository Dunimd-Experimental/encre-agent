
## Anti-Patterns

| Don't                                                                      | Instead                                                                           |
| -------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Two texts visible during a zoom-through                                    | Hard cut at blur peak, one text at a time                                         |
| 20px blur on text-scale subjects                                           | 10px text; 18–20px only full-frame                                                |
| Inverse-zoom exit → grow-from-small entry (or push → oversized retraction) | Match the scale-velocity SIGN; verify at cut±0.1s                                 |
| Incoming comp's own scale-up intro under a Z-seam wrapper tween            | Arrive composed; stage entrances after the seam settles or match the sign         |
| Mismatched blur/opacity at the swap                                        | Identical values at the cut frame                                                 |
| Gentle entry easing (`power2.out`)                                         | Mirror the exit: `power4.out` / `expo.out`                                        |
| Full off-screen exits/entries                                              | Partial travel (~12%) + early fade                                                |
| `.inOut` eases on either side of a cut                                     | Mirrored `power4.in` / `power4.out`                                               |
| Lone element fading long before its cut                                    | Fade ends ~0.02s before the cut, or word-cascade                                  |
| Equal gaps across a waterfall cascade                                      | Shrink gaps ×0.84 per word                                                        |
| Zoom-through on body text                                                  | Headlines and short phrases only                                                  |
| Scene cuts without cut-the-curve                                           | It is the default boundary                                                        |
| Consecutive boundaries in opposing directions                              | One current; reserved vectors spent on meaning                                    |
| Unpainted `#root` behind a mid-window cut                                  | Opaque stage ground                                                               |
| Queued entries (each waits for the previous to settle)                     | Overlap ±1–2 frames — the cascade is a wave, not a queue                          |
| Same offset/duration for every cascade element                             | Vary by weight: anchors travel further, punctuation snaps                         |
| Gradual opacity fade on a §6 arrival                                       | Binary 0→1 via `tl.set` — fading fights the snap (seam cuts fade; arrivals don't) |
| Single ease for a group slide (`power4.inOut`, `slow()`)                   | The §7 three-phase chain                                                          |
| Nudge tail shorter than 3× the ramp-in                                     | Extend the tail's TIME, not its distance                                          |

## Code

All GSAP templates — worker-authored versions, registry `gsap_template`s, the combined
cut-the-curve + zoom, waterfall DOM/CSS/JS, rack-focus — live in
`examples/gsap-implementation.md`.
