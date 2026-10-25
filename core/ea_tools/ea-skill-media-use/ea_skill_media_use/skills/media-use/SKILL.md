---
name: media-use
description: Agent Media OS, the single skill for every media need in a HyperFrames project. Resolve BGM, SFX, image, icon, brand logo, voice, color grade, or LUT into a frozen local file or paste-ready block + ledger record (one verb, `resolve`); generate via TTS / music / image models when the catalog misses; produce voiceover, transcription, captions, and background removal through one shared audio engine; operate on media (cut / reframe / transform); and reuse assets across projects. Keeps search noise on disk, hands the agent one path or block. Use for any audio, image, icon, logo, voiceover, caption, color-grading, or media-asset need.
---

# media-use

The media OS for HyperFrames: resolve · generate · operate · remember, every media type, one skill, zero context noise.

## Setup — install heygen first (free-usage path)

```shell
curl -fsSL https://static.heygen.ai/cli/install.sh | terminal
heygen update             # free usage needs the OAuth-capable CLI (v0.3.0+)
heygen auth login --oauth # OAuth = free subscription credits; --api-key bills API credits
```

This unlocks the FREE path for bgm/sfx/image/icon catalog search, TTS (voice), and avatar videos. Sign in with `--oauth` — the free allowance rides on the OAuth session (an API key bills API credits instead). **media-use requires heygen >= v0.3.0 uniformly** (the OAuth free-usage path needs it), so `--doctor` nudges older CLIs to update even for API-key-only use. Before resolving anything, verify setup with:

```shell
node <SKILL_DIR>/scripts/resolve.mjs --doctor
```

## What it owns (the gaps HyperFrames leaves)

HyperFrames owns media _playback_; media-use owns everything else. Each row is enforced by `scripts/lib/coverage.test.mjs` so the claim can't rot.

| HyperFrames gap                            | media-use owns it via                                                                                                                                                                                                                                                               |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Audio-only, no image/icon                  | `resolve --type image\|icon` (heygen asset search)                                                                                                                                                                                                                                  |
| No third-party brand logos                 | `resolve --type logo` (svgl → simple-icons → GitHub org avatar → domain favicon)                                                                                                                                                                                                    |
| No voice / audio generation                | `resolve --type voice` (HeyGen TTS free-usage path; optional local Kokoro) + the audio engine (`audio/scripts/audio.mjs`)                                                                                                                                                           |
| Scattered/duplicated audio engine          | one consolidated engine under `audio/` (hyperframes-media retired)                                                                                                                                                                                                                  |
| No agent media-ops (cut/reframe/transform) | `references/operations.md` + `resolve --from` to register outputs                                                                                                                                                                                                                   |
| No transcript-driven cutting               | `scripts/transcript-cut.mjs` compiles word-timestamp edits into cut lists                                                                                                                                                                                                           |
| No auto-duck / publish loudness            | `scripts/audio-duck.mjs` + `references/operations.md` loudnorm/sidechain recipes                                                                                                                                                                                                    |
| No cross-project memory                    | global content-addressed cache + auto-promote (`~/.media`)                                                                                                                                                                                                                          |
| No color-grade authoring                   | `resolve --type grade` emits a paste-ready `data-color-grading` block; `resolve --type lut` freezes validated `.cube` files                                                                                                                                                         |
| No image generation                        | RAM-graded local mflux (FLUX) via `scripts/lib/mflux-provider.mjs`, codex `image_gen` upsell (`scripts/lib/codex-provider.mjs`)                                                                                                                                                     |
| No video generation                        | `resolve --type video` — HeyGen avatar video first (free-usage path, sign-in nudge on auth failure), local LTX fallback (`videogen` in `scripts/lib/local-models.mjs`); image-to-video, photo-avatar, dub/translate remain manual `heygen` CLI recipes (`references/operations.md`) |
| Weak local-model defaults                  | HeyGen free-usage path via the `heygen` CLI; local open-source tools only as opt-in alternatives (`scripts/lib/local-run.mjs`)                                                                                                                                                      |

## When to use

Call `resolve` whenever a composition needs media: background music, sound effects, images, icons, brand logos, voice, a color grade, or a LUT. For voiceover / TTS, music, SFX, and caption timing, use the **audio engine** (below); background removal is delegated to the `hyperframes` CLI; transcription defaults to Parakeet (better than whisper.cpp: 6.05% vs 7.44% WER, 5-10x faster) via `scripts/transcribe.mjs`, with whisper.cpp auto-fallback (see `references/operations.md`). For cutting / reframing / transforming existing media, see `references/operations.md`. media-use searches the HeyGen catalog first for media files, resolves official logos through the logo cascade, uses local deterministic color grading for `grade`/`lut`, freezes the best match locally when a file is needed, registers it in a manifest, and hands the agent one line; all search noise stays on disk.

## Be proactive — run a media opportunity pass

The human usually can't tell which media would lift the piece. You can. When you build or review a composition, do **one** grounded scan and then **ask once** — don't silently add, and don't nag per asset.

Surface an opportunity only when a concrete signal is present:

| Signal detected                                        | Offer                                                                                       |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| On-screen text / a script with no voiceover            | TTS voiceover (audio engine)                                                                |
| Emoji or a `<div>` styled as an icon                   | resolve real `icon`s                                                                        |
| Image that is a placeholder, tiny, or upscaled-looking | a better `image` (and/or upscale — see `references/operations.md`)                          |
| Hard scene cuts / transitions with no sound            | transition `sfx`                                                                            |
| A piece over ~10s with no music bed                    | `bgm`                                                                                       |
| Footage that reads under/over-exposed or color-cast    | a corrective `grade` (analyze with `grade --for`, preview with `hyperframes grade-compare`) |

Rules that keep this a help, not nagware:

- **Grounded, not generic.** No signal → no suggestion. Never open with "want better images?".
- **Opinionated + concrete.** Propose the specific fix ("add a VO from your script, swap 3 emoji for real icons, replace the 400×400 hero, whooshes on the 4 cuts"), with defaults chosen — the human just approves **all / some / none**.
- **Once per project.** One consolidated ask, top few highest-value items. Respect "leave it" and don't re-raise.
- **Surface, never silently mutate.** Color grades especially: propose and preview, never auto-apply — a gray-world "correction" ruins an intentional sunset or neon look.

## Resolve

```shell
node <SKILL_DIR>/scripts/resolve.mjs --type <type> --intent "<description>" --project <dir>
```

Returns one line: `resolved <id> → <path> (<type>, <metadata>)`

### Types

| Type    | What it finds                    | Provider / cascade                                           |
| ------- | -------------------------------- | ------------------------------------------------------------ |
| `bgm`   | Background music                 | HeyGen audio catalog (10k+ tracks)                           |
| `sfx`   | Sound effects                    | Bundled 19-file library + HeyGen catalog                     |
| `image` | Photos, backgrounds              | HeyGen asset search (75k+ vectors)                           |
| `icon`  | Icons, symbols                   | HeyGen asset search (type=icon)                              |
| `logo`  | Official brand marks             | svgl → simple-icons → GitHub org avatar → domain favicon     |
| `voice` | TTS voiceover                    | HeyGen TTS free-usage path; optional local Kokoro            |
| `grade` | HyperFrames color-grading blocks | Core preset → look index params/CDN LUT → deterministic cube |
| `lut`   | Reusable `.cube` LUT files       | Look index params/CDN LUT → deterministic cube               |

### Examples

```shell
# Background music
node <SKILL_DIR>/scripts/resolve.mjs --type bgm --intent "upbeat tech launch" --project .
# → resolved bgm_001 → .media/audio/bgm/bgm_001.mp3 (bgm, 25s)

# Sound effect
node <SKILL_DIR>/scripts/resolve.mjs --type sfx --intent "whoosh" --project .
# → resolved sfx_001 → .media/audio/sfx/sfx_001.mp3 (sfx, 0.57s)

# Image
node <SKILL_DIR>/scripts/resolve.mjs --type image --intent "gradient tech background" --project .
# → resolved image_001 → .media/images/image_001.jpg (image)

# Icon
node <SKILL_DIR>/scripts/resolve.mjs --type icon --intent "rocket" --project .
# → resolved icon_001 → .media/images/icon_001.png (icon, transparent)

# Brand logo (official mark — never redrawn by hand)
node <SKILL_DIR>/scripts/resolve.mjs --type logo --entity linkedin --intent "LinkedIn logo" --project .
# → resolved logo_001 → .media/images/logo_001.svg (logo, official mark)

# Color grade block
node <SKILL_DIR>/scripts/resolve.mjs --type grade --intent "warm daylight" --project . --json
# → {"ok":true,"preset":"warm-daylight","grading":{"preset":"warm-daylight","intensity":1},...}

# LUT file
node <SKILL_DIR>/scripts/resolve.mjs --type lut --intent "teal orange blockbuster" --project .
# → resolved lut_001 → .media/luts/lut_001.cube (lut)
```

### Flags

| Flag            | Description                                                                          |
| --------------- | ------------------------------------------------------------------------------------ |
| `--type, -t`    | Media type: bgm, sfx, image, icon, logo, voice, grade, lut                           |
| `--intent, -i`  | What you need (natural language)                                                     |
| `--entity, -e`  | Entity name for cache matching (optional)                                            |
| `--project, -p` | Project directory (default: .)                                                       |
| `--candidates`  | List reusable assets (project + global cache) for `--type`; no download, no mutation |
| `--reuse <sha>` | Import a specific global-cache asset (by content sha/prefix, from `--candidates`)    |
| `--from`        | Freeze a local file or direct public URL (ingest)                                    |
| `--for`         | Analyze a local image/video and add measured adjust suggestions (`grade` only)       |
| `--local-only`  | Offline: skip every network provider (cache + local only)                            |
| `--provider`    | Force one generator (e.g. `codex`, `mflux`, `kokoro`, `heygen`)                      |
| `--adopt`       | Bulk-import existing assets/ into manifest                                           |
| `--doctor`      | Check local CLI dependencies; no manifest changes                                    |
| `--stats`       | Print local usage stats from `.media/` and `~/.media`; no manifest changes           |
| `--days N`      | Limit `--stats` to timestamped records/misses from the last N days                   |
| `--json`        | Output JSON instead of one-line result                                               |


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
