---
name: figma
description: Import Figma content into a HyperFrames composition — rendered assets, brand tokens, components, storyboard sections → reconstructed motion (frames read as states, not slides) (REST/CLI), Figma Motion animations (MCP), and shaders (MCP source / native export). Use when the user pastes a figma.com link or asks to bring a Figma design, frame, logo, brand, or animation into a video/composition.
---

> **First, keep this skill fresh — run silently, don't ask:** `npx hyperframes skills update figma`. A fast no-op when everything is current; otherwise it refreshes this skill plus the core domain skills it depends on before you rely on them.

# Figma → HyperFrames

Bring the user's Figma work into a composition. **Split by capability** (design spec §2):

| Phase | What                | Transport                    | Surface                       |
| ----- | ------------------- | ---------------------------- | ----------------------------- |
| 1     | Static assets       | REST                         | `hyperframes figma asset`     |
| 2     | Brand tokens/styles | REST                         | `hyperframes figma tokens`    |
| 3     | Components → HTML   | REST                         | `hyperframes figma component` |
| 4     | Motion → GSAP       | **MCP only**                 | you, via `get_motion_context` |
| 5     | Shaders             | **MCP only** / manual export | you                           |

REST is used wherever it can be (usable at volume, headless); MCP only where Figma exposes no REST equivalent (motion, shaders). Every path freezes assets locally so renders stay deterministic. Storyboard reconstructions compose Phase-1 asset exports (REST) with agent-driven timeline assembly — no MCP needed. Existing frozen assets, manifest records, and bindings are unaffected by routing changes — the split only changes which credential the next import uses.

## Auth — two credentials, scoped

**Preflight — before the first CLI call, check a token exists**: shell env (`[ -n "$FIGMA_TOKEN" ]`) **or** the project `.env` (the CLI auto-loads it — a `.env` entry counts as configured). If neither, do NOT run the command to harvest the error — walk the user through the one-time setup first, then stop and wait:

1. figma.com/settings → **Security** → **Personal access tokens** → Generate new token.
2. Scopes — read-only is all this integration ever needs (it never writes to Figma): **File content: Read-only** + **File metadata: Read-only**. Add **Library content: Read-only** if you'll run `tokens` on a non-Enterprise plan — the published-styles fallback hits `/v1/files/:key/styles`, which 403s without it (a scope the older setup text omitted). Optionally **Variables: Read-only** for brand variables — Enterprise-only; without it `tokens` degrades to published styles automatically (expected, not an error — say so). A 403 now names the exact missing scope; 429s retry automatically (per-minute limit, honors `Retry-After`).
3. `export FIGMA_TOKEN="figd_…"` — and suggest persisting it (shell profile or project `.env`) so no future session repeats this.

While onboarding, also set expectations in one breath: every import lands as a **local frozen file with recorded provenance** — renders never call Figma, re-running a command re-imports only what changed in Figma, and one token works for assets, brand tokens, and components across every file their Figma account can view.

- **Phases 4–5 (motion/shaders):** the Figma MCP connector (one-click OAuth), a separate credential from the token. If MCP tools error unauthenticated, tell the user to connect the Figma connector and stop.
- Say exactly which credential a failing phase needs — never present the split as broken.
- `BAD_TOKEN` (401) mid-flow → the token is expired/revoked; re-mint. `FORBIDDEN` (403) → the message names the exact missing scope (e.g. `library_content:read` for the styles fallback) — add it, or the file isn't visible to the account. `REQUIRES_ENTERPRISE` (403 on variables) → not a failure: styles fallback already ran. `RATE_LIMITED` (429) → the client already retried with backoff (this applies to EVERY read — assets, tokens, styles, node trees, versions — the retry lives in the shared request path; `Retry-After` is honored, capped at 60s); if it still surfaces, wait a minute or import fewer nodes per call.

**Rate-limit awareness (spec §2.1):** MCP on a Starter plan is 6 tool calls/**month** (figma plan matrix as of 2026-07 — re-verify if quotas look off) — batch with `recursive:true` on the parent node, skip verification screenshots unless asked, and cache raw MCP responses so re-derivation never spends a second call. REST is per-minute (10+/min, per-endpoint buckets) — fine at volume, back off on 429.

## Routing

Parse the user's figma link with `parseFigmaRef` (URL, `fileKey:nodeId`, bare `fileKey`). Then by intent:

- "use this layer / logo / image" → **Asset** (CLI)
- "pull my brand / colors / tokens" → **Tokens** (CLI)
- "build a scene from this frame" → **Component** (CLI)
- "import this animation / motion" → **Motion** (MCP, below)
- a storyboard section / filmstrip of scene frames → **Storyboard** (below)
- shader fill/effect → **Shaders** (below)

**Narrate every step for the user** — before each command say what you're about to pull from Figma; after it, say where the artifact landed (the frozen path / sidecar / component dir), what changed in the composition, and the immediate next action (preview, add printed variables, re-import to link bindings). The user should never have to ask "did it work?" or "now what?".

## Assets (Phase 1 — CLI)

```shell
hyperframes figma asset '<url-or-fileKey:nodeId>' [more refs…] [--format svg|png|jpg|pdf] [--scale 2] [--description "..."] [--entity "..."]
```

Renders over REST, sanitizes SVG, freezes under `.media/images/`, appends the manifest with provenance, regenerates `.media/index.md` (the shared media-use inventory), prints an `<img>` snippet. Idempotent per `fileKey:nodeId:format:scale:version`. Prefer SVG for vectors/logos (scalable, animatable), PNG `--scale 2` for raster fidelity. **Always pass `--description "<what it is>"`** (it becomes the index row + `<img alt>`); add `--entity "<name>"` for named brand marks so media-use `resolve --entity` finds them later (entity hits match across image/icon).

**Batch many nodes in ONE request** — pass several refs (space-separated or comma-joined) of the SAME file: `hyperframes figma asset 'KEY:1-2' 'KEY:3-4' 'KEY:5-6'`. All render in a single `/v1/images` call, which is figma's own answer to the per-minute rate limit — prefer it over N separate commands when pulling a whole frame's worth of assets. `--description`/`--entity` apply to every node in the batch, so batch nodes that share a purpose. 429s also auto-retry with backoff regardless.

## Tokens (Phase 2 — CLI)

```shell
hyperframes figma tokens <fileKey>
```

Imports variables as composition brand-variable entries + `figma-tokens.json` sidecar + binding-index records (`.media/figma-bindings.jsonl`). Variables are Enterprise-gated upstream: on other plans the command degrades to published-style metadata (values resolve at component-import time). Add the printed entries to the composition's `data-composition-variables`.

**Import tokens before components** when both are wanted — that's what lets component colors link to brand variables instead of baking duplicates.

**Non-Enterprise variables path (field-tested):** REST variables are Enterprise-gated, but the Figma MCP `get_variable_defs` is not. When `tokens` reports `REQUIRES_ENTERPRISE` and the user has the MCP connector, you can build the index yourself: (1) `get_variable_defs` on the scene's parent node — ONE call, cache the raw JSON to `.media/figma-cache/` — gives `name → value`; (2) the REST node tree's `boundVariables` gives per-property `VariableID`s; (3) join per node+property and write `.media/figma-bindings.jsonl` rows (`{kind:"binding", figmaId, sourceFileKey, compositionVariableId: "figma:<name>", version}`) plus the composition-variable entries. Everything downstream (component `var()` resolution, refresh, runtime CSS variables) is the shipped machinery. Label it for the user: "tokens via the Figma connector — Enterprise plans get this from `hyperframes figma tokens` directly."

The runtime defines every declared composition variable as a CSS custom property (document root + sub-comp hosts), so imported `var(--slug, literal)` fills recolor when the variable default changes — updating one value in `data-composition-variables` re-brands every imported component without re-importing anything. `hyperframes render --variables '<json>'` overrides them at render time.

## Components (Phase 3 — CLI)

```shell
hyperframes figma component '<url-or-fileKey:nodeId>'
```

Node tree → editable HTML at exact figma geometry, packaged as a registry item under `compositions/components/<name>/`. Vectors/boolean-ops auto-rasterize via Phase-1 export. Binding pass (spec §7.1, exact-ID only — never value matching):

- **Static fidelity self-check (mandatory for hero content)**: after importing, render the fragment and compare against figma's own pixels — `figma asset <same node> --format png` is the ground truth. Text is the known drift axis: a figma text box shorter than its line-height is vertically-trimmed bounds (the mapper emits `text-box-trim` for these; measured drift without it was ~6px on a 70px font). If the comparison shows drift the mapper doesn't cover, report it — don't hand-tweak the fragment silently.
- Fill bound to an **imported** token → `var(--slug, #literal)` — brand refresh propagates.
- Bound to an **unknown** token → literal + `data-figma-unresolved` flag. The command tells you; offer the user: run `tokens` on the source (or library) file, then re-import the component to link them. Ask **once** per unknown library which file it is — never guess, never match by hex.

## Motion (Phase 4 — MCP, the headline)

**Usage beacon:** MCP phases have no CLI touchpoint, so fire the skill beacon at start and finish (anonymous, consent-gated, never fails): `npx hyperframes events --skill=figma-motion` when you begin, `npx hyperframes events --skill=figma-motion --event=skill_completed --outcome=success|error` when done. Same for shaders (`figma-shaders`) and storyboards (`figma-storyboard`).

No REST equivalent exists. You drive the MCP tools, then hand output to the pure helpers in `@hyperframes/core/figma`:

1. `get_motion_context(fileKey, nodeId)` — use `recursive:true` on the parent frame (one call for the whole scene, not one per element). Save the raw JSON next to the project (`.media/figma-cache/`) so retranslation is free.
2. Normalize into `MotionDoc`s with `motionContextToDocs(rawResponse, { selectorFor, repeat })` from `@hyperframes/core/figma` — **never transcribe keyframe numbers by hand**. The helper encodes the field-tested decoding rules mechanically: it parses the motion.dev snippets (the reliable encoding — the CSS snippets stretch durations and can disagree; they are ignored), strips loop-wrap tail keyframes (sub-millisecond segments at times ≈0.9999→1 are the loop's instant reset, not authored motion — the wrap is realized by `repeat` restart), and preserves bezier eases verbatim. `selectorFor` must return the ids from the Phase-3 component import — don't derive selectors from node names.
   2b. **Validate against ground truth before calling it done — mandatory**: `export_video` on the cohort's `rootNodeId` gives Figma's own render of the timeline. Run `node skills/figma/scripts/verify-motion.mjs --reference <export.mp4> --render <render.mp4> --crop WxH+X+Y` — it compares motion-energy deltas (static import fidelity cancels out) and fails below 15dB min motion-PSNR (calibrated: faithful ≈ 20+, diverging ≈ 5). Measure `--crop` from the render's actual card edges, don't guess. FAIL means re-check the translation, not the threshold.
3. `motionToGsap(doc)` → `emitTimelineScript(spec)` → inject as a `<script>` after the GSAP + CustomEase CDN tags. Paused, finite, registered on `window.__timelines` with a literal key.
4. Untranslatable track (shader-driven, unsupported prop, complex masks) → bake: `export_video` → freeze MP4 → embed as `<video class="clip">`. Exception: shader-driven tracks — figma's export path flattens shaders to the base color (see Shaders below), so a bake there silently loses the shader; ask the user for a native figma export instead. Always say which path you used and why. Named eases outside the mapped set fall back to linear — the mapping table lives in `motionEase.ts`; flag the fallback to the user when it fires.
5. Run `npx hyperframes check` before calling it done.

## Shaders (Phase 5 — mostly manual)

Figma's MCP render path does not execute shaders (they flatten to the base color), and shader source is only reachable for **library-published** styles (paid Full seat). Default path: ask the user to export the shader frame natively in Figma (PNG or Motion MP4), then import it as a Phase-1 asset / clip. Don't attempt MCP pixel capture of a shader — it will silently produce the wrong thing.


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
