---
name: theme-factory
description: "Apply one of 10 curated color-plus-font themes to slides, docs, reports, or landing pages, or generate a custom theme on the fly. Use for consistent, professional styling of any artifact."
user_invocable: true
hidden: true
context: inline
---

# Theme Factory

A library of ten professional font and color themes. Each theme ships as a small markdown spec in `themes/` with a color palette (hex codes, with the role of each color), a header/body font pairing, and the contexts it suits. Pick one and apply it consistently to whatever artifact you are building — slides, documents, reports, HTML landing pages.

## When to Use

- The user asks for a "professional look", a brand feel, or a consistent style across an artifact.
- You are about to produce slides, a report, a landing page, or a document and want the styling decided up front.
- The user reacts to a specific mood ("warm", "corporate", "minimal", "botanical") and wants it as a palette.

## When NOT to Use

- The user just needs raw content, not styling → `write-docs`, `doc-coauthoring`.
- Visual art or a poster → `canvas-design`.
- Charts only → `chart-visualization` (it has its own styling; still use a theme's palette for the series colors if the deck has one).
- An existing brand with a locked style guide → apply the brand guide, not a preset theme.

## Tools You Use

| Task | Tool | How |
|------|------|-----|
| Show all themes visually | `file_read` | Read `theme-showcase.pdf` (never modify it) and show it to the user |
| Read a theme spec | `file_read` | `themes/<kebab-name>.md` |
| Apply to a deck | `presentation` | `action: "create"` / `add_slide"`; set fill colors and font names explicitly |
| Apply to a document | `document` | `create` / `add_text` / `add_table` |
| Apply to HTML | `file_write` | Inline CSS custom properties with the hex values |
| Confirm the output | `file_read` / `presentation` | Re-read the file or its text and check the colors landed |

## The Ten Themes

| Theme | File | Character |
|-------|------|-----------|
| Ocean Depths | `themes/ocean-depths.md` | Professional, calming maritime |
| Sunset Boulevard | `themes/sunset-boulevard.md` | Warm, vibrant sunset |
| Forest Canopy | `themes/forest-canopy.md` | Natural, grounded earth tones |
| Modern Minimalist | `themes/modern-minimalist.md` | Clean, contemporary grayscale |
| Golden Hour | `themes/golden-hour.md` | Rich, warm autumnal |
| Arctic Frost | `themes/arctic-frost.md` | Cool, crisp, winter |
| Desert Rose | `themes/desert-rose.md` | Soft, sophisticated dusty tones |
| Tech Innovation | `themes/tech-innovation.md` | Bold, modern tech |
| Botanical Garden | `themes/botanical-garden.md` | Fresh, organic garden colors |
| Midnight Galaxy | `themes/midnight-galaxy.md` | Dramatic, cosmic deep tones |

Each spec lists its colors with roles — for example Ocean Depths gives Deep Navy `#1a2332` as the primary background, Teal `#2d8b8b` as the accent, Seafoam `#a8dadc` as the secondary accent, and Cream `#f1faee` for text and light backgrounds — plus a header/body font pairing and a "Best Used For" line.

## Workflow

1. **Show the showcase.** Read `theme-showcase.pdf` and show it as-is so the user can see every theme at once. Do not edit it.
2. **Recommend before you ask.** Based on the audience and content, suggest one or two themes with a one-line reason, then ask which to use.
3. **Wait for an explicit choice.** Do not guess; a theme is a brand decision.
4. **Read the spec file.** Take the hex codes and both font names verbatim.
5. **Apply consistently.** Same background, same accent role, same pairing, on every page — no drifting a single color per slide.
6. **Verify.** Re-read the output and confirm the palette actually landed; contrast must be readable.

## Custom Theme

When none of the ten fits, create one:

1. Name it in the same spirit — evocative and descriptive, two or three words.
2. Define four colors with roles: primary background, accent, secondary accent, text/light surface.
3. Choose a header font and a body font that complement each other.
4. Write it in `themes/` using the exact shape of the other specs (heading, one-line description, color palette bullets with hex and role, typography, best-used-for).
5. Show it for review and get sign-off before applying it.

## Application Rules

- Readability over flavor: the text color must stay legible on the background at every size.
- Use the accent sparingly. It highlights, it does not fill.
- Do not invent extra colors; if you need more, derive shades of the four.
- Keep the pairing fixed: header font only on headers, body font everywhere else.
- Dark backgrounds need light text and muted secondary accents, not pure white at full saturation.

## Examples

```json
{ "tool": "presentation", "action": "create", "file_path": "report.pptx",
  "title": "Q3 Review", "slides": [ { "title": "Q3 Review", "content": "Overview" } ] }
```

```html
<!-- theme as CSS custom properties -->
<style>
  :root { --bg: #1a2332; --accent: #2d8b8b; --accent-2: #a8dadc; --text: #f1faee; }
  body { background: var(--bg); color: var(--text); font-family: "DejaVu Sans"; }
  h1, h2 { font-family: "DejaVu Sans Bold"; }
</style>
```

## Best Practices

- Decide the theme before writing content; restyling later is wasted work.
- Carry the palette across every artifact in the same engagement (deck + report + one-pager).
- Reuse the theme file as the single source of truth; never retype hex codes from memory.
- When content is data-heavy, lean on Modern Minimalist, Arctic Frost, or Tech Innovation.
- When the audience is emotional or lifestyle-oriented, lean on Sunset Boulevard, Golden Hour, or Botanical Garden.

## Common Pitfalls

- Editing `theme-showcase.pdf` to make a point — it is a read-only catalogue.
- Applying colors but leaving the fonts default, which breaks the identity half.
- Inventing a fifth color "just for this slide".
- Choosing a theme for its name instead of its readability on the actual content.
- Mixing two themes in one deck because the accent "felt better" on one slide.

## Anti-patterns

- Picking a theme after the deck is finished and restyling piecemeal.
- Treating the palette as decoration rather than a system with roles.
- Skipping the user's choice and applying the "nice-looking" one anyway.

## Pairing with Other Skills

- `pptx` / `presentation` — the most common consumer of a theme.
- `document`, `write-docs`, `report` — reports and memos.
- `chart-visualization`, `beautiful-mermaid` — feed the palette in as series and node colors.
- `canvas-design` — for a standalone poster, use a theme's palette as inspiration only.
