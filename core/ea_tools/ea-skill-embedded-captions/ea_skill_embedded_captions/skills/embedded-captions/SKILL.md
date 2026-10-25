---
name: embedded-captions
description: >
  Add captions or subtitles to an existing single-subject talking-head video without editing the
  footage. Use for plain verbatim captions, cinematic captions embedded behind the subject, VFX
  captions, "explosive/VFX/cool subtitles," or a named identity from the 36-style catalog. Route by visual
  identity, not by backend engine. The quiet `anchor` rail is the default; embed every word only
  when the user explicitly wants a fully cinematic treatment. The workflow runs locally end to end,
  including transcription and subject matting; split multi-shot footage before applying it.
---

> **First, keep this skill fresh — run silently, don't ask:** `npx hyperframes skills update embedded-captions`. A fast no-op when everything is current; otherwise it refreshes this skill plus the core domain skills it depends on before you rely on them.

# Embedded Captions

**One catalog, picked up front** ([CATALOG.md](CATALOG.md) — 36 identities; the engines behind it are backend detail). **Standard** (default) builds a clean verbatim **rail** (lower-third subtitle carrying most text) + an **embed** climax composited _into_ the scene behind the subject at the peak. **Cinematic** is pure embed — no rail, every caption composited behind the subject (hero typography, accumulation, occlusion as the effect). **Theme** is a complete themed constitution — body paradigm × hero setpiece × front fx × plate reaction, composed from registries ([themes/README.md](themes/README.md)): `ordnance` `terminal` `neonsign` `stardust` `stomp`. Most explainer / voiceover is **Standard**; **embed is the scarce, earned peak** — embedding every word is the common mistake; Theme is for VFX-grade asks ("explosive", "VFX", "like AE did it").

---

## Operational flow (TL;DR)

Routed through `/hyperframes`, the intent layer confirms only the input (which clip) and **announces** the identity pick as a deferred ask — the shortlist needs the probed clip, so it stays at step 1 below; the layer's run-shape questions don't apply (the footage is untouched, there is no storyboard to review). A `BRIEF.md`, when present, carries the confirmed input and any user notes — read it first.

The craft prose below is long; the **pipeline itself is short** — and everything deterministic is computed or compiled, never hand-written:

1. **Decision gate** (refuse bad clips) → **pick ONE identity from [CATALOG.md](CATALOG.md)** (36 identities; engine/compiler derived by lookup — never surface a mode/category question)
2. `hyperframes init` (skip it if the project dir already exists with the video inside — `matte.cjs`/`transcribe.cjs` adopt any video in the dir as source.mp4) → **`terminal scripts/prepare.sh <project>`** (matte ∥ transcribe ∥ audio-envelope in parallel, then safe-zones v2 with scene palette/optics/lighting — one command, nothing forgotten)
3. **author a small JSON of creative choices** (read `safe-zones.json` first): Cinematic → `plan.json` → `fill-timings.cjs` → `fit-fonts.cjs` → `make-composition.cjs`; Theme → `theme.json` → `make-theme.cjs` (rail/panel/poem/takeover paradigms; `anchor` is the quiet rail default)
4. **Visual QA**: `node scripts/preview-frames.cjs <project>` → faithful composite previews in ~2s/frame (no render). Check § Visual QA before paying for a render.
5. `render-and-composite.sh` → gates (timing / occlusion+hero / overflow / hand-off) → `final.mp4`

Load-bearing rules people miss:

- **rail (default) + embed (promotion).** `drop` (filler, not shown) / `rail` (verbatim lower-third subtitle, in front, carries most text) / `embed` (a peak word composited behind the subject). **Standard mode does both**, embedding only the peak(s). See **§ Caption model**.
- **The video is delivered UNTOUCHED (Standard/Cinematic; **Theme mode's PLATE budget is the one sanctioned exception** — register-gated reaction beats (charge-dim, punch, shake, grain) defined per theme DNA and applied AFTER the matte composite so subject+text+plate move as one frame)** — captions are the only thing added; the matte just lets the subject occlude the embed track. Never grade/recolor/scanline the footage.
- Two rulebooks: **rail → [references/rail.md](references/rail.md)** (thin), **embed craft → [references/composition-craft.md](references/composition-craft.md)** (rich, embed-only). Skim by need.

---

## Caption model — rail + embed

Every spoken phrase is one of three things:

|           | What                                             | How it's shown                                                                                                                                                    |
| --------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **drop**  | filler — um/uh, stutters, self-corrections       | not shown                                                                                                                                                         |
| **rail**  | the default — ordinary spoken content (verbatim) | clean lower-third subtitle, **in front**, readable. A punch word can get an inline `emphasis` highlight (accent colour / active-word pop) — it stays on the rail. |
| **embed** | a promoted peak — the headline beat              | one big word composited **behind the subject** (matte occlusion), designed entrance + exit                                                                        |

**The rail carries most of the text; embed is the scarce, earned peak.** Scarcity is **per beat/block, not per clip**: ≤1 hero per block (thought), never two co-visible, ≥ a beat of air between hero windows (the compiler warns under 0.6s). A short clip → usually 1–2; a long explainer → ~one per section. Among multiple heroes, the **largest authored one is the APEX** (it alone gets the full lockup embed + width-fit raise); smaller ones are **MINOR peaks** that ride their column as oversized emphasis lines (fg, damped motion) — not every beat needs the matte showcase, which is exactly what keeps the apex an event. Embedding every word is still the common mistake.

Rail-surface identities build exactly this (rail = `rail.html`, embed = the climax in `index.html`). Column-flow identities drop the rail and make everything embed-style — recommend them only for mood-over-verbatim asks, never for explainer / voiceover where the words must read (CATALOG.md encodes this per identity).

---

## Step 0 — pick ONE identity from the CATALOG

**One front-end, three engines behind.** The user picks an IDENTITY from [CATALOG.md](CATALOG.md) (36 entries: 10 classic + 26 themed); the engine, compiler and authoring file are derived by lookup from the catalog row. **Never surface "Standard vs Cinematic vs Theme" as a question** — those are backend names (a product has one UX even with several engines). The catalog encodes everything routing needs: reading surface, voice, recommend-for, scene needs, adjacency notes for the genuinely-close pairs (loud↔ordnance, neon↔neonsign, cream↔stardust).

The identity pick is a **preference gate** (`../hyperframes-core/references/brief-contract.md` § 1): in autonomous mode ("surprise me" / "decide for me"), pick from your shortlist yourself and state the one-line why instead of asking.

Procedure: probe the clip → shortlist 2–3 identities from the catalog → recommend ONE with a one-line why → **the user picks** (autonomous mode: you pick, stating the why) → author that identity's file. Identities are engine-locked (no cross combos; opening one is a validation event — see dna/README.md).

**Always present your recommendation and let the user pick before you author.** Don't silently default.

(The full identity table lives in [CATALOG.md](CATALOG.md) — single source of truth for routing. The engine docs below describe each backend's authoring contract.)

**Recommendation heuristic**: use the "Shortlisting heuristics" in [CATALOG.md](CATALOG.md) — they are identity-level (e.g. "explosive" shortlists ordnance/stomp/terminal/loud and picks by WHAT should explode), never category-level. Unsure → `anchor`.

- **Cinematic** → write `plan.json` for a locked template, compiled by `make-composition.cjs`.
- **Theme** → read [themes/README.md](themes/README.md), author `theme.json`, run `scripts/render-theme.sh` (compiles + renders + plate reaction → **final_fx.mp4**).

---

## Decision gate — RUN FIRST

Probe the video and classify the scene before either mode.

```shell
ffprobe <video.mp4>                    # specs
ffmpeg -ss <t> -i <video.mp4> -vframes 1 sample.png   # at 20/50/80%
```

Read the samples. Refuse if:

- Multiple speakers / hard cuts (split & render each shot, or refuse)
- No human subject (this skill is for talking-head)
- Under 3 seconds, **no speech**, or face never clearly visible — `transcribe.cjs` warns when audio is near-silent (Whisper hallucinates words like "Thank you." over silence); **heed it and refuse** rather than caption fabricated words
- **Source already has burned-in captions / subtitles / heavy text graphics** — adding a second caption system conflicts and the footage ships untouched (no covering/inpainting). Burned text often appears only mid-clip: sample a **1fps contact sheet** (`ffmpeg -i in.mp4 -vf "fps=1,scale=160:-1,tile=10x5" sheet.png`), don't trust 3 spot frames.
- **Transcript is garbage** — non-native/heavy-accent speech can transcribe into confident gibberish. Sanity-read `transcript.json` before authoring; if it doesn't parse as language, try `WHISPER_MODEL=medium` once, else refuse (a verbatim rail of fabricated words is worse than no captions).
- Busy handheld with fast motion (matte flickers)

### Pre-flight probes (cost nothing, prevent the worst failures)

1. **Shot-cut probe.** Sample frames at 20%, 50%, 80%. If a different subject/scene appears, **trim the clip** before the cut.
2. **Letterbox / pillarbox probe.** Black bars on the first frame? Compute safe content rect and constrain caption placement inside it.
3. **Luminance probe.** Sample the caption region's average luminance — `under 60` → light text reads as-is, `60-180` → add the glyph scrim, `180+` → opaque text + scrim (never bare light text). **Cinematic templates are cream+`screen` and LOCKED** — use this probe to _pick a fitting identity_ (bright scenes → `ink`, or the opaque-rail `anchor` theme), never to recolour one.
4. **Identity recommendation by tone (you recommend; the user picks — see Step 0 + CATALOG.md).** explainer / interview / must-read words → rail/panel-surface identities; poetic / social / "cinematic" → column-flow identities by register; "explosive / VFX effects / VFX" / named worlds → themed identities. When unsure → `anchor` (words read, scene safe) — but present a shortlist and let the user choose.

---

## Pipeline — 5 steps

```
1. hyperframes init <project> --non-interactive --video <video.mp4>
2. terminal scripts/prepare.sh <project>       # matte ∥ transcribe (parallel) → safe-zones. One command.
                                           #   → frames_fg/ transcript.json safe-zones.json
3. [AGENT STEP — the only creative step] author a small JSON; see below by mode
   Cinematic: author plan.json → node scripts/fill-timings.cjs → fit-fonts.cjs → make-composition.cjs
   Theme:     author theme.json → terminal scripts/render-theme.sh <project>   (compiles + renders + plate fx)
4. node scripts/preview-frames.cjs <project>   # ~2s/frame composite previews → § Visual QA (BEFORE the render)
5. terminal scripts/render-and-composite.sh <project>  # gates → final.mp4 + history/ snapshot
   (Theme mode: SKIP steps 3b/5 — render-theme.sh already runs compile + render-and-composite
    + _postfx.sh; the deliverable is final_fx.mp4, final.mp4 is pre-plate-reaction)
```

Step 1's `init` checks the installed skills against the latest on GitHub and updates the global set if any are out of date.

Step 3 differs by mode:

### Step 3 — Cinematic mode (pure embed)

1. **Read `safe-zones.json` first.** Narration planes go in **`zones.hugLeft`/`hugRight`** — clean strips ABUTTING the silhouette (text far from the body reads as floating, not embedded; far corners are the fallback, not the default). The hero defaults to `heroAnchor`/`heroBands.best` (centered ON the subject, ~30–55% occluded). `recommendation:"fg"` moves NARRATION in front for legibility; **the hero stays embedded whenever `heroBands.feasible`** — hero-fg is the last resort.
2. **The DNA is the identity you picked in Step 0** (CATALOG.md) — do not re-open the choice here. Sanity-check it against the scene (bright hero band luma > 150 wants `ink`; full pick guidance lives in the catalog, covering all ten incl. neon / glitch / chrome / velocity). State your pick + why; the user decides. The DNA locks type/palette/blend/motion + hero three-act; safe-zones v2 (`palette`/`optics`/`lighting`) parameterizes it to THIS scene automatically.
3. **Author `<project>/cinematic.json`** — `"dna": "<name>"` + thought-BLOCKS, not raw groups: each block = lines of words (grouped 2–5 at clause boundaries) + the plane it stacks in + per-line `css` (size/weight/style only — no positions) + at most ONE line marked `"hero": true` (the promoted word; `"text"` for display form). Schema: `scripts/make-cinematic.cjs` header.
4. **Compile**: `node scripts/make-cinematic.cjs <project>` — lowers blocks → plan.json → index.html. Generated for you: transcript-sequenced timings, accumulate-within-block, page-flip-between-blocks, **the hero LOCKUP** (a hero block's pre-context, HERO and post-context stack as ONE bonded composition centered on the subject — reading order top→bottom = spoken order by construction; context floats in FRONT while the hero embeds BEHIND = the depth sandwich; a mass rule keeps the hero dominating its context), apex/minor hero split, **reading order by construction**, fg fallback per safe-zones. Then the gates run as usual. _(Hand-authoring plan.json directly remains possible for designs blocks can't express — then run `fill-timings.cjs` + `fit-fonts.cjs` + `make-composition.cjs` yourself.)_

### Step 3 — Theme mode (themed constitution)

**Read [themes/README.md](themes/README.md) FIRST** — paradigm/setpiece registries, linkages, hard rules, and the exact `theme.json` schema.

1. **Pick a theme DNA** by content register (each `themes/<name>.json` has `voice` + `when`). State your pick + why; the user decides.
2. **Author `<project>/theme.json`** — `dna`, `lines` (verbatim, transcript order; 1–5 words each — for `takeover` each line is one CARD), `minors` (emphasis words), `hero:{match}` (the climax word/phrase; leave it OUT of `lines` for embed setpieces, keep it IN for inline setpieces and panel+redact).
3. **Render**: `terminal scripts/render-theme.sh <project>` — compiles (verbatim-completeness gate at compile time), renders both layers, composites, applies the plate reaction → `final_fx.mp4`. Use `preview-frames.cjs` between compile and render for Visual QA.

---


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
