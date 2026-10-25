---
name: talking-head-recut
description: Package an existing talking-head / interview / podcast video with timed, designed GRAPHIC OVERLAY cards — kinetic titles, lower-thirds, data callouts, quotes, side panels, picture-in-picture — synced to the transcript, on a 16:9 / 9:16 / 4:5 canvas of your choice; the clip plays untouched underneath. Trigger on "graphic overlays", "on-screen graphics", "package / dress up my video". Not plain subtitles (/embedded-captions). Unclear → /hyperframes.
---

> **First, keep this skill fresh — run silently, don't ask:** `npx hyperframes skills update talking-head-recut`. A fast no-op when everything is current; otherwise it refreshes this skill plus the core domain skills it depends on before you rely on them.

# Talking Head Recut

Talking Head Recut takes a local video that **plays in full** and layers a sequence of
timed, designed **graphic cards** onto it — titles, lower-thirds, data callouts,
quotes, side panels, picture-in-picture — synced to what's being said. The agent
designs the cards (timing + content) and **writes each card's HTML directly in the
conversation**, then assembles a single composition HTML and renders it to MP4 via
`hyperframes`. There is no fixed archetype list and no prescribed card structure —
the overlays emerge from what the transcript actually says.

> **The front door is `/hyperframes`.** This skill packages an **existing talking-head clip** with **designed graphic cards** (titles, lower-thirds, data callouts, quotes, side panels, PiP) — not plain captions (the spoken words as text). **The clip plays untouched.** Any other intent — plain subtitles, a standalone graphic, a from-scratch video — or any uncertainty → read `/hyperframes` first: the intent layer owns every route decision.

> **Graphic-packaging sibling of `embedded-captions`.** Captions add the _spoken words_
> as a readable subtitle; this adds _designed graphics_ on top of the playing video.
> Plain subtitles → `embedded-captions`. Build a video from scratch → the creation
> workflows (`product-launch-video` / `faceless-explainer` / …).

Routed through `/hyperframes`, the intent layer confirms only the input (which clip) and **announces** the render-strategy questions as deferred asks — aspect, layout, style group, and card count stay at Step 7, where the probed footage and transcript ground the recommendations; the layer's run-shape questions don't apply. A `BRIEF.md`, when present, carries the confirmed input and any user notes — read it first.

Inspectable intermediate files in the work directory:

- `metadata.json` — duration / width / height / fps
- `audio.mp3` — extracted audio
- `transcript.json` — a flat **word array** `[{ text, start, end }, …]` (Whisper; no `segments`, no `words` wrapper)
- `storyboard.json` — lightweight card outline (the agent's plan)
- `public/cards/card-XX.html` — one HTML fragment per card
- `public/index.html` — final assembled composition
- `output.mp4` — rendered video

## CLI Resolution

```shell
# hyperframes — transcription (local Whisper) + rendering the assembled HTML to MP4
npx hyperframes --help
```

This skill runs entirely on the **hyperframes** CLI plus system `ffmpeg` / `ffprobe`.
Transcription is local **Whisper** via `hyperframes transcribe` — no third-party
service, API key, or rate-limited proxy.

## Workflow

### 1. Check Environment

```shell
npx hyperframes doctor          # ffmpeg, headless browser, render deps
# confirm bundled assets:
ls "<SKILL_DIR>/assets/fonts" "<SKILL_DIR>/assets/vendor/gsap.min.js"
```

Required:

- `ffmpeg` / `ffprobe` (system)
- `<SKILL_DIR>/assets/fonts/*.woff2`, `<SKILL_DIR>/assets/vendor/gsap.min.js` (bundled inside this skill, staged to work dir in Step 9)

Transcription needs no key — `hyperframes transcribe` runs Whisper locally (Step 4).

Strongly recommended on macOS for `hyperframes render`:

```shell
export PRODUCER_BROWSER_GPU_MODE=hardware
```

### 2. Create a Work Directory

All artifacts live under `videos/<project-name>/` — the same convention as the other
video workflows (`product-launch-video` / `faceless-explainer` / `pr-to-video`). Keep
the cwd at the workspace root; everything below writes under this one subdirectory.

```shell
VIDEO_PATH="/absolute/path/input.mp4"
WORK_DIR="videos/$(basename "$VIDEO_PATH" | sed 's/\.[^.]*$//')"
mkdir -p "$WORK_DIR"
```

### 3. Extract Audio and Metadata

```shell
# metadata — duration / width / height / fps
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate \
  -show_entries format=duration -of json "$VIDEO_PATH" > "$WORK_DIR/metadata.json"
# audio
ffmpeg -y -i "$VIDEO_PATH" -vn -acodec libmp3lame -q:a 2 "$WORK_DIR/audio.mp3"
```

Outputs: `metadata.json` (read `width`/`height`/`duration`; fps = the `r_frame_rate`
fraction evaluated, e.g. `30000/1001 → 29.97`) + `audio.mp3`.

### 4. Transcribe

```shell
npx hyperframes transcribe "$WORK_DIR/audio.mp3" -d "$WORK_DIR" --json --model small.en
```

Local **Whisper** — no API key, no proxy, no rate limit. Writes a word-level
`transcript.json` into the work dir (word `text` + `start` / `end` timestamps).
Read it for the word / sentence timings that drive card timing in Step 6; group
words into sentences yourself at punctuation / pauses if you need segment-level
chunks.

**Clamp to media duration.** Whisper can return the final word's `end` a hair past the
actual clip length — clamp every card `endSec` and `composition.durationSeconds` to the
`metadata.json` duration, or the render will show a black tail past the video.

### 5. Correct Transcript

`transcript.json` is a **flat array of word objects** — `[{ "text": "...", "start": s, "end": s }, …]` (no `segments` array, no `words` wrapper; the per-word key is **`text`**). Read it and fix obvious ASR errors:

- Homophones, product names, technical terms, punctuation
- Edit a word's `text` in place; **preserve its `start` / `end`** timestamps
- There is no pre-grouped `segments` array — **group words into sentences yourself** (split at terminal punctuation / pauses) when you need segment-level chunks for card timing

### 6. Draft a Lightweight Storyboard (in chat)

**No CLI involved.** Read `transcript.json` + `metadata.json` and design
cards directly. `storyboard.json` is an agent-internal planning artifact
— no CLI command consumes it; it exists so you can think clearly
about timing and content before writing each card's HTML. Keep the
shape consistent with the example below so the same outline can drive
the composition you author in Step 9:

```json
{
  "schemaVersion": 3,
  "composition": {
    "fps": 30,
    "width": 1080,
    "height": 1920,
    "durationSeconds": 121.2,
    "layout": "portrait",
    "themeId": "noir",
    "seed": 42
  },
  "videoTrack": {
    "sourcePath": "input-video.mp4",
    "startSec": 0,
    "endSec": 121.2,
    "bounds": { "x": 0, "y": 0, "width": 1080, "height": 1920 }
  },
  "subtitles": { "enabled": false },
  "cards": [
    {
      "id": "card-01",
      "intent": "Hook with the speaker's anxious midnight question",
      "startSec": 0.5,
      "endSec": 13.0,
      "accentIndex": 0,
      "zone": "fullscreen",
      "contentHints": {
        "kicker": "AN HONEST QUESTION",
        "title": "The soul-searching question at 11 PM",
        "detail": "Client's 60-second voice message: 'If the RMB appreciates, does that mean my USD policy is a terrible loss?'"
      }
    }
  ]
}
```

**Required Card fields:**

| field                   | type                                       | purpose                                                                                               |
| ----------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `id`                    | string                                     | stable id used in card HTML & GSAP selectors                                                          |
| `intent`                | string                                     | natural-language description; fed to card synthesis                                                   |
| `startSec` / `endSec`   | number                                     | times in seconds (endSec > startSec)                                                                  |
| `accentIndex`           | 0 \| 1 \| 2 \| 3 \| 4                      | which of the 5 theme accent colors this card pulls                                                    |
| `zone`                  | enum (see below)                           | where on the canvas the card lives                                                                    |
| `contentHints`          | object                                     | free-form bag; agent puts kicker/title/detail/data/quote here                                         |
| `archetype` (optional)  | string                                     | free-form label you may attach to remember a card's pattern; absent = free-form, which is the default |
| `transition` (optional) | enum: `cut` \| `fade` \| `slide` \| `wipe` | declarative card-to-card transition                                                                   |

**Five `zone` values:**

| zone              | resolved bounds                                | when to use                             |
| ----------------- | ---------------------------------------------- | --------------------------------------- |
| `fullscreen`      | covers whole canvas                            | hero moments, big numbers, mantras      |
| `whiteboard-area` | inset 40px margin (or 45% of portrait height)  | dense data / annotated content          |
| `lower-third`     | bottom 30% band                                | annotation over visible video           |
| `side-panel`      | right 42% (landscape) or bottom 40% (portrait) | data side, video other side             |
| `video-overlay`   | full canvas, expects mostly-transparent card   | annotation overlays on full-bleed video |

When you assemble the composition in Step 9, resolve each card's `zone`
into pixel bounds on the card-host wrapper following the table above.
Video bounds are set **once** at composition level (`videoTrack.bounds`);
to make video appear to "move between cards", author GSAP tweens against
`#video-wrap` in the composition's `<script>` (see Step 9).

**No prescribed card roles, no prescribed narrative arc.** Cards emerge
from what the video actually says — could be all quotes or all data,
could open with a number or with a story. Let the transcript drive the
rhythm.

**How many takeaways? — auto-infer from duration + density.** No fixed
upper limit. Pick a **base pace** from the video duration, then adjust
by **information density**. Only **floor is fixed: minimum 5 cards** so
even short videos have rhythm.

**Step 1 — base pace by duration** (the natural sec/card for medium density):

| video duration     | base pace (sec per card) | rationale                                   |
| ------------------ | ------------------------ | ------------------------------------------- |
| < 60s (short reel) | **6–8s**                 | viewers expect fast cuts in short-form      |
| 60s – 3 min        | **8–12s**                | normal social pace                          |
| 3 – 10 min         | **12–20s**               | give breathing room; each card carries more |
| 10 – 30 min        | **20–35s**               | long-form lecture / interview rhythm        |
| > 30 min           | **30–60s**               | episodic, near-chapter feel                 |

**Step 2 — density multiplier** (multiplies the base pace):

| signal in the transcript                                                                                                    | multiplier | effect                   |
| --------------------------------------------------------------------------------------------------------------------------- | ---------- | ------------------------ |
| **High density** — many numbers, distinct claims, staccato pacing, list-like enumeration, every 1–2 sentences is a new idea | **× 0.7**  | cuts faster, more cards  |
| **Medium density** — mixed flow with both data and narrative                                                                | **× 1.0**  | base pace                |
| **Low density** — one extended story, repeated reframing, slow reflective pacing, single argument unfolding                 | **× 1.5**  | cuts slower, fewer cards |

**Step 3 — compute:**

```
secPerCard = basePace × densityMultiplier
cardCount  = max(5, round(videoDurationSec / secPerCard))
```

Examples (notice — **no upper clamp**; long videos naturally produce more cards):

- **30s reel, single punchline (low density)** → 7 × 1.5 = 10.5s/card → round(30/10.5)=3 → floor to **5** cards
- **60s reflective monologue (low density)** → 10 × 1.5 = 15s/card → **4** → floor to **5** cards
- **121s talking-head with rich data (high density)** → 10 × 0.7 = 7s/card → **17** cards
- **5 min interview, mixed density** → 16 × 1.0 = 16s/card → **19** cards
- **10 min deep-dive, high density** → 16 × 0.7 = 11s/card → **55** cards
- **30 min lecture, medium density** → 28 × 1.0 = 28s/card → **64** cards
- **1 hr podcast, low density** → 45 × 1.5 = 67.5s/card → **53** cards

When a card holds longer than ~15s, plan for a richer card (data block,
multi-step reveal, several sub-points unfolding with staggered
animations) — a static one-liner gets boring past 8s. For long pieces
where many cards exceed 30s, consider **chunking the timeline into
sub-compositions** (one .html per chapter, mounted with
`data-composition-src`) so the GSAP timeline per file stays manageable
— see the `timeline_track_too_dense` HyperFrames lint warning.

`content` can be a plain string ("Title: annualized 5.69%\nNotes: ...") or any JSON
shape that captures the data. The agent decides the shape per card.

**Optional outro.** This skill ships **no fixed brand outro**. If the user wants a closing card, design a neutral one yourself (wordmark + one-line tagline, ~1.5-2s, fade in -> short hold -> fade out), append it to `cards[]`, and extend `composition.durationSeconds` to its `endSec`. Otherwise end on the last content card.

### 7. Decide Render Strategy

#### Confirm Visual Direction with User (DO THIS FIRST)

Before you start designing cards or deciding bounds, **ask the user to
pick the output ratio, the layout, the style, and the card-density
preset**. Frames are auto-selected from the chosen layout × style
combination (see "Auto-pick frame" table below). Before sending the
question, **precompute two things**:

1. **`recommendedRatio`** from the source video's aspect ratio
   (`metadata.json` width / height):
   - `sourceAspect = width / height`
   - `sourceAspect ≥ 1.5` (≥ ~3:2 wide) → recommend **`16:9`**
   - `sourceAspect ≤ 0.7` (≤ ~9:13 tall) → recommend **`9:16`**
   - `0.7 < sourceAspect < 1.5` (near-square) → recommend **`4:5`**

   Mark the recommended option's label with " (recommended · matches source video X:Y)"
   so the user sees why it's recommended.

2. **`autoCount`** from Step 6 (`max(5, round(videoSec / (basePace ×
densityMultiplier)))`) so the "auto" option's label can show the
   concrete number.

**Environment compatibility — pick the best available question channel.**
Not every runtime exposes the same structured-question tool. Apply this
order:

1. **`AskUserQuestion`** (Claude Code, Anthropi

---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
