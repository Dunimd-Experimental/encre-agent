---
name: web-design-guidelines
description: "Audit UI code against the Web Interface Guidelines: accessibility, focus rings, keyboard handling, form semantics, motion, dark mode, copy. Use for review-my-UI, check-accessibility, audit-design requests."
---

# Web Interface Guidelines Review

Reviews components and pages against the Web Interface Guidelines rule set
and returns terse, actionable findings instead of an essay.

## When to Use

- "Review this page/component for UX and accessibility issues."
- Before shipping a UI change, as a pre-merge check.
- A user reports keyboard, focus or screen-reader problems and wants a systematic sweep.
- You need a compliance pass over many files at once (a route, a design system folder).

## When NOT to Use

- The user wants a visual redesign or new layout — that is `frontend-design` or `canvas-design`.
- Pure performance work (bundle size, layout shift, Core Web Vitals) — use `vercel-optimize` or `react-best-practices`.
- Backend or API review — this skill only reads UI code.
- No code exists yet; writing new UI is not an audit.

## Workflow

1. **Fetch the current rules.** They live upstream and change; do not rely on memory:

   ```
   web_fetch(url="https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md", format="markdown")
   ```

   If `web_fetch` fails, retry once through `terminal` (`curl -sL <url>`). If both fail, say the rule set is unavailable and audit only against the categories below — never invent rule numbers.

2. **Determine the target files.** Use the argument as a path or glob (`src/components/**/*.tsx`). If the user named no files, ask rather than auditing the whole repository.

3. **Read the files.** Use `file_read` for individual files; `grep` for rule-specific sweeps (e.g. `onClick` on non-interactive elements, `outline: none`, missing `alt`).

4. **Check every rule against the fetched list**, including the ones that are easy to miss: visible focus rings, `aria-*` correctness, label association, reduced-motion handling, contrast, destructive-action confirmation, loading and empty states, and copy quality.

5. **Report in the guidelines' output format** — typically one finding per line as `path/to/file.tsx:42 — rule — what is wrong — suggested fix`. Group by file, order by severity (blocking accessibility first).

6. **Stop at findings.** Do not rewrite the files unless the user explicitly asks for the fixes as well.

## Example

```
web-design-guidelines src/app/settings/page.tsx
```

Result shape:

```
src/app/settings/page.tsx:18 — focus-visible — icon button has no visible focus ring — add focus-visible:ring-2
src/app/settings/page.tsx:44 — form-label — input relies on placeholder as label — add <label for>
```

## Best Practices

- Quote the rule text or id from the fetched guidelines, so the finding is verifiable.
- One finding per line; no preamble, no summary paragraph per file.
- Separate "violates a rule" from "subjective preference" — the second belongs in a short note, if at all.
- When a rule cannot be judged statically (real screen-reader output, motion feel), say so instead of guessing.

## Common Pitfalls

- Auditing from memory of the guidelines and reporting outdated rules.
- Turning the audit into a refactor: hundreds of changed lines the user did not ask for.
- Reporting every `div` with an `onClick` as an error when it is a legitimate delegated handler — check for `role`, `tabIndex` and key handling first.
- Missing the dark-mode and reduced-motion passes because the file "looked fine".

## Anti-patterns

- "Looks good overall" with no line references.
- Inventing rule numbers to sound authoritative.
- Marking accessibility issues as minor because the visual design is polished.
