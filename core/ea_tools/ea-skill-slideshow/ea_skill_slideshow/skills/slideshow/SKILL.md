---
name: slideshow
description: "Author a HyperFrames slideshow - a navigable deck with discrete slides, fragment reveals, branching hotspots, speaker notes, and presenter mode. Output is a running deck, not an MP4."
user_invocable: true
hidden: true
context: inline
argument_hint: what the deck is about
---

# Slideshow

A HyperFrames slideshow is a normal HyperFrames composition — scenes, clips, GSAP timelines — plus one extra ingredient: a **JSON island** that declares which scenes are slides and how they connect. The player's `SlideshowController` reads the island and turns the continuous timeline into a discrete, navigable deck.

The full authoring contract, schema tables, and a complete worked example live in `references/authoring-contract.md`. Read that file before authoring anything non-trivial; this file is the load-bearing summary.

## Before You Author

**Refresh the skill (silent, do not ask):** `npx hyperframes skills update slideshow` — a fast no-op when current.

**Figma source:** if the content or storyboard comes from a `figma.com` URL, run the `figma` skill first for asset export, brand tokens, and storyboard reconstruction, then build from its output. Do not drive Figma through raw MCP tools — that skips SVG sanitization, `.media/manifest.jsonl` provenance, and brand-token `var()` binding.

**Intent confirmation.** If the user explicitly asked for a slideshow or pitch deck, proceed. If this skill was triggered by something adjacent ("presentation", "convert this page"), pause and ask a short yes/no question first:

> Do you want this as a HyperFrames slideshow?

Frame it briefly: a runnable deck with discrete slides, built-in navigation and presenter mode, editable speaker notes, shared media handling, and validation before handoff. For page conversions, also note that the goal is to preserve the original page's design, motion, and media behavior while translating page movement into slide-to-slide transitions. Do not implement until they say yes. This is a routing decision, not a preference gate — it survives autonomous "surprise me" mode.

A `BRIEF.md`, when present, carries the confirmed intent — read it.

## Output: a Deck, Not an MP4

Serve it with `hyperframes present <project-dir>` (or Studio present mode). **Never `hyperframes render` a slideshow into one MP4.** A deck is several top-level scene compositions with no master-root wrapping them, so `render` resolves only the first composition and silently emits a truncated file (6 seconds of a 40-second deck). Supported outputs today are the live `present` deck and per-slide `snapshot` stills. If the user needs a linear MP4, surface that limitation instead of pointing `render` at the deck.

## The Two Pieces

**1. Scenes, declared the normal way.** Every slide is backed by a scene:

```html
<div data-composition-id="problem" data-start="0" data-duration="8"
     data-label="The problem" data-width="1920" data-height="1080">
  <!-- clips go here -->
</div>
```

Branch slides (reachable only via a hotspot, excluded from the main line) are declared exactly the same way — they appear only in a `slideSequences` entry.

**2. The JSON island.** Exactly one `<script type="application/hyperframes-slideshow+json">` block per composition, near the top of `<body>`, before the scene divs:

```html
<script type="application/hyperframes-slideshow+json">
{ "slides": [...], "slideSequences": [...] }
</script>
```

The island is the single source of truth for slide order, notes, fragment hold-points, hotspots, and branch sequences. Do not build it at runtime with an alternate `application/json` block — `present` reads the composition HTML statically.

### Shapes

```json
{
  "sceneId": "problem",
  "notes": "Lead with the pain, not the company.",
  "fragments": [3.5, 5.2, 7.0],
  "hotspots": [
    { "id": "h1", "label": "How did we calculate this?",
      "target": "market-deep-dive", "region": { "x": 60, "y": 10, "w": 35, "h": 20 } }
  ]
}
```

- `sceneId` — required; must match a scene's `data-composition-id` exactly.
- `notes` — presenter only, never shown to the audience.
- `fragments` — absolute composition-timeline seconds within the slide's `[start, end]`.
- `hotspots[].target` — must match a `SlideSequence.id`; `region` is a 0-100 percentage box; omit it to render a full-slide labeled button.
- `startTime` / `endTime` — optional overrides of the matched scene's bounds.
- `ttsScript`, `ttsAudioUrl`, `ttsDurationMs` — reserved; TTS playback is not wired. Omit.
- `slideSequences[]` items are `{ "id", "label", "slides": [SlideRef...] }`; fragments and nested hotspots are allowed inside.

## Slide Writing Rules

Hard constraints, not suggestions:

- **Headline is a complete-sentence claim, not a label.** "SMBs spend 14 hours/week on manual scheduling", not "Scheduling problem".
- **One idea and one visual per slide.** Second bullet cluster or second chart means split the slide.
- **Lead with the punchline** — strongest point first, on the slide and in deck order.
- **Bottom-up market sizing only.** Never "$50B TAM" without the math (accounts × ACV, transactions × take-rate).
- **Minimum font 30pt equivalent.** At 1920×1080, headline 72-96px, body 48px; nothing the audience must read goes below 40px.

## Fragments and Branching

A fragment is an absolute timeline time where the controller holds a reveal state. Entering a fragmented slide seeks to `fragments[0]` and holds; Next seeks to the next fragment; after the last one, Next advances slides. A slide without fragments enters at a rest frame near its midpoint, not at `slide.end`. Navigation is seek-driven, never play-driven — design states so they are correct at the target time. Fragment times outside `[start, end]` are a lint error.

Branching pushes `{sequenceId, slideIndex: 0}` onto the nav stack and enters the branch's first slide; `back()` returns to the exact parent slide; `backToMain()` clears the stack to the root.

## Validation

```shell
npx hyperframes lint
npx hyperframes check
```

Treat lint errors and runtime `StaticGuard` contract messages as blockers even if a command exits successfully. Rerun until lint reports `0 error(s)`. The slideshow lint rule checks that every `slide.sceneId` resolves to a scene, every `hotspot.target` names a defined sequence, and fragment times fall inside their slide's range.

## Handoff

The root `index.html` must be a runnable slideshow entrypoint: opening it in a browser shows navigation and responds to Next/Prev. If the raw composition must stay separate for CLI compatibility, put it under `composition/` and point commands there:

```json
{ "scripts": {
    "dev": "npx hyperframes present ./composition",
    "studio": "npx hyperframes preview ./composition" } }
```

Rely on the built-in Present button rendered by `<hyperframes-slideshow>` — no bespoke `#present-btn`. If `file://` restrictions break iframe media or same-origin access, use a self-contained wrapper or start a local server; never leave `index.html` broken. At handoff give the presenter URL and the one line: "Click Present, or press P, to open the audience tab." Keep the server running if you started it.

## Common Pitfalls

- Rendering the deck to MP4 and reporting a truncated file as done.
- Writing labels instead of claims, which kills the deck at slide one.
- Offsetting fragment times from the scene start instead of using absolute timeline time.
- Leaving `var(--f-body)` as a font family, or `preload="none"` on media.
- Hiding inactive scene frames with `opacity: 0` only — invisible layers still swallow clicks; set `opacity`, `visibility`, and `pointer-events` together.
- Skipping `lint` + `check` because the browser looked right.

## Anti-patterns

- Building the island at runtime behind an alternate script type.
- Solving a generic slideshow behavior with a one-off deck script instead of the player.
- Authoring without confirming the deck is what was asked for.

## Pairing with Other Skills

- `hyperframes-core` — base composition contract: clips, tracks, `data-*` attributes, determinism.
- `figma` — design source, assets, brand tokens.
- `theme-factory` — palette direction for the deck's look.
- `pptx` — for a static deck the user wants to edit in PowerPoint, not a runnable one.

## Sources

- Upstream: skills.sh `heygen-com/hyperframes/slideshow`
- Full contract: `references/authoring-contract.md`, `references/standalone-harness.md`
