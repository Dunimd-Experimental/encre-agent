
## Storyboards (a SECTION of scene frames → animation)

**The cardinal rule: storyboard frames are KEYFRAMES, not slides.** Two frames containing the same element describe that element's state through time — animate the ELEMENT between the states; never play the frames as a sequence of stills. A logo drawn in four consecutive frames at descending y is ONE element rising through four keyframes. Playing storyboard frames back-to-back is the failure mode; reconstructing the element timelines they imply is the job.

Storyboard files follow a grammar you can parse mechanically — don't eyeball, decode:

1. **Scene units**: inside the SECTION, every frame-sized node is a scene — both named FRAMEs _and_ loose full-frame RECTANGLEs (designers paste stills straight into the section). Filter by size (≈ composition aspect, e.g. >1400×900), not by node type or name.
2. **Order = x-position** (row-major if the strip wraps). Sort scenes by `absoluteBoundingBox.x`.
3. **Diff adjacent frames into element chains** — this is where the animation lives. Match children across consecutive frames: first by **name** (same name = same element → tween its relative x/y/w/h between states), then by **geometry similarity** (similar size + nearby center = same logical element whose pixels changed → crossfade the two exports in place while tweening geometry; covers typed-text progressions and morph states). Unmatched children enter/exit at their scene's beat. Frame background fills tween as a color track. Export ONE asset per chain (one per state only when pixels genuinely differ) — never one still per frame.
4. **Stills are the fallback, not the default** — only for frames that don't decompose (flat full-frame screenshots with no shared elements); those get the animatic treatment below.
5. **Director notes**: TEXT nodes below the strip are motion intent, paired to the scene whose x-range they overlap. They describe _how_ to animate — they are not on-screen copy.
6. **Batch exports** (elements or stills): `GET /v1/images` accepts comma-separated ids, but big scene frames hit "Render timeout" past ~12 ids — chunk to ~4 per call with a retry. (One call per scene wastes the rate budget; 26 scenes ≈ 52 calls via the single-asset path.)
7. **Note verbs → transitions** (starter vocabulary, extend as encountered):

| Note says                       | Do                                                                |
| ------------------------------- | ----------------------------------------------------------------- |
| EXPLOSION / BURST               | incoming scale ~1.5→1 + fade, `power3.out`                        |
| SLIDES / SLIDE TO THE… / SCROLL | directional slide in from that edge                               |
| MORPH / REVEALS                 | crossfade — or Phase-3 import if the motion is inside one scene   |
| CYCLE THROUGH / EACH ONE        | longer hold — or Phase-3 import if items animate within the scene |
| (no note)                       | crossfade + slow Ken-Burns drift                                  |

8. **Stills vs. components routing**: a note describing motion _between_ scenes → transition on the still (above). A note describing motion _inside_ a scene ("TEXT LINES REVEAL ONE AFTER THE OTHER", "PILLS ANIMATE IN") → that frame deserves a Phase-3 component import (real elements) animated per the note, not a flat PNG. Do the animatic pass first with stills, then upgrade the scenes the notes single out.
9. One `main` timeline sequences everything (opacity/x/y per scene at absolute times) — no per-scene sub-compositions needed for an animatic.
10. **Escalation — frames depict ONE product UI → rebuild the app, not element chains.** When every frame is the same application screen in successive states (a signup flow, a settings panel, a player), element chains undersell it. Rebuild the UI as live DOM — Phase-3 component import for the parts that change state, real exported pixels for static chrome (**code what changes state, freeze what doesn't**) — and treat each frame delta as an **interaction to perform**, not a tween to apply: the cursor enters, clicks the control, the state responds, screens push/slide as real navigation. The result reads as one continuous screen recording of a working app. This is the cardinal rule taken to its conclusion for UI flows; the stills/element-chain treatments are for storyboards that aren't one coherent application.

## Determinism

Never leave a Figma URL in the composition — freeze first. Never emit `repeat: -1`. Timelines paused, finite, literal `window.__timelines` keys. All Figma I/O at import time; render sees local files only.
