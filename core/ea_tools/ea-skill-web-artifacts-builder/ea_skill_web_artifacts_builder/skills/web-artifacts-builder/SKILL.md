---
name: web-artifacts-builder
description: "Build complex interactive HTML artifacts with React, TypeScript, Tailwind, and shadcn/ui, then bundle to one self-contained HTML file. Use when the deliverable needs state, routing, or rich UI components."
user_invocable: true
hidden: true
context: inline
---

# Web Artifacts Builder

Turn an idea into a single self-contained `bundle.html` that runs anywhere in a browser. The stack is React 18 + TypeScript + Vite, Tailwind CSS, shadcn/ui on top of Radix UI, bundled with Parcel and inlined with html-inline.

Use this skill for artifacts that genuinely need component architecture: stateful apps, multi-view dashboards, editors, calculators, planners, data viewers. For a plain static page or a chart, write HTML directly — no build step.

## When to Use

- The user wants an interactive tool: a tracker, calculator, planner, dashboard, editor, quiz, or viewer.
- The deliverable must be one portable HTML file they can open or share.
- It needs forms with state, multiple views or routes, or a real component library.

## When NOT to Use

- A simple single-file HTML/CSS page with no logic → write it straight to a file with `file_write`.
- A landing page that must look designed → `frontend-design`.
- A chart or dashboard over real data → `chart-visualization` for the image, `data-analysis` for the numbers.
- A deployed website → build it normally and use `deploy-to-vercel` or `iga-pages`.
- Slides or a presentation → `pptx`, `slideshow`.

## Tools You Use

| Task | Tool | How |
|------|------|-----|
| Initialize the project | `terminal` | `terminal scripts/init-artifact.sh <name>` in this skill directory |
| Edit source files | `file_write` / `file_edit` | Components, pages, styles inside the generated project |
| Inspect the tree | `terminal` | `ls -R src/` |
| Bundle to one file | `terminal` | `terminal scripts/bundle-artifact.sh` → `bundle.html` |
| Typecheck/build errors | `terminal` | Read the Parcel output; fix and re-bundle |
| Preview and verify | `browser` | Open `bundle.html` and interact with it |
| Hand it over | `file_read` | Confirm `bundle.html` exists and report its path |

Both scripts require `terminal`, `node` (18+), and `npm`. `init-artifact.sh` extracts the bundled shadcn components archive and installs the toolchain; `bundle-artifact.sh` installs Parcel, builds without source maps, and inlines everything. The project must have an `index.html` at its root or bundling fails.

## Workflow

1. **Initialize.**
   ```shell
   terminal scripts/init-artifact.sh my-artifact
   cd my-artifact
   ```
   You get React + TypeScript via Vite, Tailwind 3.4.1 with shadcn theming, the `@/` path alias, 40+ pre-installed shadcn components, all Radix dependencies, a Parcel `.parcelrc` with alias support, and Node 18+ version pinning.

2. **Build the artifact.** Edit the generated source: add components under `src/`, wire state with React hooks or context, use shadcn/ui components instead of reinventing inputs and buttons, style with Tailwind utilities. Keep data local unless the user asks for a real backend — a bundled artifact has no server.

3. **Bundle.**
   ```shell
   terminal scripts/bundle-artifact.sh
   ```
   The result is `bundle.html`, fully self-contained: all JavaScript, CSS, and assets inlined.

4. **Deliver.** Give the user the `bundle.html` path so they can open it in any browser. Do not describe it abstractly — point at the file.

5. **Test only if needed.** Testing before delivery adds latency; skip it unless the user asks or a defect appears. If you do test, load `bundle.html` in `browser` and exercise the interactions.

## Design Rules

Avoid the "AI slop" look, which is what makes an artifact feel disposable:

- No default-centered everything. Use asymmetry, grids, and real layout intent.
- No purple-to-indigo gradients as decoration.
- No uniform rounded corners on every element.
- No Inter as the default typeface; pick a typeface with a point of view and use it consistently.
- Prefer the theme's palette when `theme-factory` has been applied to the engagement.
- Ship real density: functional content and working interactions beat large empty hero sections.

## Examples

**A habit tracker with state and persistence:**

```tsx
// state + localStorage, so progress survives a reload
const [habits, setHabits] = useState<Record<string, boolean[]>>(() =>
  JSON.parse(localStorage.getItem("habits") ?? "{}")
);
```

**Bundle and hand over:**

```shell
terminal scripts/bundle-artifact.sh
ls -l bundle.html
```

## Best Practices

- Decide the deliverable shape first: single view, tabbed, or routed. Overbuilding routing is the most common waste.
- Reuse shadcn components rather than hand-rolling accessibility-heavy widgets.
- Keep the bundle fast to open: avoid pulling in heavy libraries for small jobs.
- Persist user input locally (`localStorage`) when the artifact is a tool the user will return to.
- Verify the bundle opens from the filesystem — some browser APIs need `https://` or localhost.

## Common Pitfalls

- Running the scripts with `sh` instead of `terminal`.
- Forgetting `index.html` at the project root, which breaks bundling.
- Editing files in `scripts/` — they are scaffolding, not product code.
- Shipping an artifact that silently does nothing because state never writes back.
- Reporting success without opening `bundle.html` at least once.
- Building a login, upload, or API layer that cannot exist inside a static file.

## Anti-patterns

- Using this scaffold for a two-paragraph page.
- Delivering source code when the user asked for a file they can open.
- Polishing visuals while the core interaction is broken.

## References

- shadcn/ui component docs: https://ui.shadcn.com/docs/components
- Scaffolding scripts: `scripts/init-artifact.sh`, `scripts/bundle-artifact.sh`
