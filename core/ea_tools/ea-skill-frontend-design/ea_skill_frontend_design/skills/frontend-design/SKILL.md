---
name: frontend-design
description: "Build distinctive, production-grade frontend interfaces that avoid generic AI-slop aesthetics. Use for components, pages, apps, and landing pages where the look is part of the deliverable."
user_invocable: true
hidden: true
context: inline
---

# Frontend Design

The user describes a component, page, application, or interface and may add context about purpose, audience, and technical constraints. Your job is to implement working code with exceptional attention to aesthetic detail — deliberately designed, never generic.

## When to Use

- Building a UI where appearance is a deliverable: a landing page, product page, portfolio, editor UI, dashboard shell.
- The user asks for something "distinctive", "premium", "editorial", "brutalist", "minimal", or "not AI-looking".
- Reviewing or upgrading an existing interface that feels generic.

## When NOT to Use

- Purely functional, invisible UI where the platform's own component library is the point → use that library and move on.
- Data charts and diagrams → `chart-visualization`, `beautiful-mermaid`.
- Static posters or art objects → `canvas-design`.
- Multi-view interactive artifact needing state and routing → `web-artifacts-builder`.
- Deck styling from a preset palette → `theme-factory`.

## Design Thinking

Before writing code, understand the context and commit to a **bold** aesthetic direction:

- **Purpose** — what problem does this interface solve, and who uses it?
- **Tone** — pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic, luxury, playful, editorial, brutalist, art-deco, soft-pastel, industrial. Do not split the difference.
- **Constraints** — framework, performance budget, accessibility requirements.
- **Differentiation** — the one thing someone will remember. Name it.

Choose a clear conceptual direction and execute with precision. Bold maximalism and refined minimalism both work; the key is intentionality, not intensity.

## Aesthetic Guidelines

- **Typography.** Distinctive display font paired with a refined body font. Avoid Arial, Inter, Roboto, and system defaults. Never converge on the same safe face across projects.
- **Color and theme.** Cohesive palette in CSS variables. Dominant colors with sharp accents beat evenly distributed timid palettes.
- **Motion.** High-impact moments win: one orchestrated page load with staggered reveals beats scattered micro-interactions. Prefer CSS-only for HTML; use a motion library for React. Surprise in hover states; trigger on scroll where it earns its keep.
- **Spatial composition.** Asymmetry, overlap, diagonal flow, grid-breaking elements, generous negative space or controlled density.
- **Backgrounds and detail.** Atmosphere over flat fills: gradient meshes, noise and grain, geometric patterns, layered transparency, dramatic shadows, decorative borders, custom cursors.

## Never Do

- Inter, Roboto, Arial, or system fonts as the design voice.
- Purple-to-indigo gradients on white.
- Predictable centered hero plus three equal cards.
- The same aesthetic every time — vary light and dark, font systems, and mood between projects.
- Cookie-cutter components that ignore the actual context.

## Match Complexity to Vision

Maximalist designs need elaborate code: extensive animation, layered effects, rich texture. Minimalist designs need restraint: exact spacing, precise typography, subtle detail. Elegance comes from executing the chosen vision well, not from adding more.

## Tools You Use

| Task | Tool | How |
|------|------|-----|
| Write the interface | `file_write` | One self-contained HTML file, or a component tree in the project |
| Read existing code | `file_read` / `grep` / `glob` | Understand the project before restyling it |
| Check formatting | `lint_format` | `file_path` for the touched files |
| Preview and inspect | `browser` | Open the page, check responsive breakpoints, check contrast |
| Take a screenshot | `computer` | Capture the rendered page for review |
| Deploy the result | `deploy` / `deploy-to-vercel` | Only if the user asks for a live URL |

## Workflow

1. Restate the brief in two sentences: who it is for, what it must do, what it must feel like.
2. Commit the direction: name the aesthetic, pick the display font, pick the palette, name the single memorable idea.
3. Build it working first, then refine: layout, then typography, then motion, then detail.
4. Verify in a browser, including a narrow viewport and reduced-motion settings.
5. Deliver the file path plus what makes it distinctive.

## Quality Bar

- Production-grade and functional — no placeholder buttons, no `lorem ipsum`, no disabled flows.
- Visually striking and memorable, with one idea someone can point at.
- Cohesive: every element obeys the same direction.
- Meticulously refined in detail: kerning, spacing rhythm, focus states, hover states, empty states.
- Accessible: contrast holds, focus is visible, motion respects `prefers-reduced-motion`.

## Common Pitfalls

- Committing to "clean and modern", which produces the most generic thing possible.
- Picking beautiful fonts and never checking they render at small sizes.
- Motion that fires on load forever and never settles.
- A dark theme with pure white text at full contrast — harsh and unrefined.
- Shipping without ever opening the page in a browser.

## Anti-patterns

- Averaging several aesthetic directions and calling it eclectic.
- Decorative effects that hide a broken layout underneath.
- Ignoring the stated constraints to show off.

## Sources

- Upstream: skills.sh `anthropics/skills/frontend-design`
