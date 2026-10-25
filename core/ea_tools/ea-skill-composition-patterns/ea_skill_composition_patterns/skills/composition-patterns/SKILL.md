---
name: composition-patterns
description: React composition patterns that scale. Refactor components with boolean prop proliferation, build flexible component libraries, or design reusable APIs using compound components, lifted state, and children instead of render props.
aliases:
  - compound-components
  - "react composition"
  - no-boolean-props
argument_hint: Component or component library to refactor
user_invocable: true
hidden: true
---

# React Composition Patterns

Build components whose API grows without breaking. The core discipline: configuration goes through composition, not through a growing list of props. Every boolean prop you add doubles the behavior matrix you must test, document, and reason about; every composition point you add adds one behavior that is self-describing at the call site.

## When to Apply

- Refactoring a component that has three or more boolean props.
- Building a reusable component library (date picker, combobox, tabs, dialog, toaster).
- Designing the public API of a new component.
- Reviewing component architecture where siblings fight over the same state.
- Working with compound components or context providers.

## When NOT to Use

- Two-state components (`disabled`, `open`) where the boolean is genuinely part of the component's contract — a small, stable set of flags is fine.
- Vue or Svelte code — use the `vue` skill; composition in Vue is `defineComponent`/composables, not compound components.
- Animation choreography — use the `gsap-*` skills.

## Rule Categories by Priority

| Priority | Category                | Impact | Rule prefix          |
| -------- | ----------------------- | ------ | -------------------- |
| 1        | Component Architecture  | HIGH   | `architecture-`      |
| 2        | State Management        | MEDIUM | `state-`             |
| 3        | Implementation Patterns | MEDIUM | `patterns-`          |

Fix priority 1 before anything else: an API that cannot be extended is expensive to fix later, and state rules depend on it.

## The Rules

### 1. Component Architecture (HIGH)

- `architecture-avoid-boolean-props` — Do not add boolean props to customize behavior. Replace the prop with a composition point: a child element, a separate variant component, or an optional part of the compound component.
- `architecture-compound-components` — Structure complex components as `<Picker>`, `<Picker.Trigger>`, `<Picker.Panel>`, each rendering into a shared context. Each subcomponent owns its own markup and its own responsibilities; the parent owns state.

### 2. State Management (MEDIUM)

- `state-decouple-implementation` — The provider is the only place that knows how state is stored. Consumers call actions and read values; nobody reaches into the implementation. This lets you swap useState for a library without touching consumers.
- `state-context-interface` — Define the context value as `{ state, actions, meta }` and inject it. Keep the type generic enough that a test provider can substitute its own implementation.
- `state-lift-state` — Move shared state up into the provider so siblings can coordinate. Two components reconciling state through props and callbacks is a smell; two components reading the same context is the fix.

### 3. Implementation Patterns (MEDIUM)

- `patterns-explicit-variants` — Create `PrimaryButton`, `SecondaryButton`, `GhostButton` instead of `<Button variant={...}>` with a switch. A variant switch is hidden coupling: adding a variant changes every caller.
- `patterns-children-over-render-props` — Pass children and give them context; do not pass `renderItem` or `renderTrigger` callbacks. Children are typed, discoverable, and do not break when the library version changes.

## Rule Files

Seven rule files live in `rules/` alongside this document (plus `_sections` and `_template` scaffolding):

```
rules/architecture-avoid-boolean-props.md
rules/architecture-compound-components.md
rules/state-decouple-implementation.md
rules/state-context-interface.md
rules/state-lift-state.md
rules/patterns-explicit-variants.md
rules/patterns-children-over-render-props.md
```

Each file has the same shape: why the rule matters, an incorrect example with an explanation, a correct example with an explanation, and extra context. Read the one that matches the problem — the set is small enough that reading the wrong one is cheap. The compiled guide with every rule expanded is `AGENTS.md` at the skill root.

## How to Apply a Rule

1. **Name the smell.** Boolean proliferation, a variant switch, props and callbacks wiring two siblings together, or a `renderX` prop.
2. **Find the component and every call site.** Use `grep` for the prop or the component name; a refactor of a public component is not done until no caller still passes the removed prop.
3. **Read the matching rule file** in `rules/` for the exact shape of the fix.
4. **Refactor in one direction only** — move behavior outward into composition points. Do not also clean up unrelated code in the same pass; keep the diff reviewable.
5. **Verify.** Run `lint_format` on the touched files, then check the call sites render as before. If the component is visual, load it in a real browser with the `browser` tool and exercise every variant, because a missing variant renders as nothing rather than as an error.

## Tools You Use

| Task                             | Tool       | Note                                                  |
| -------------------------------- | ---------- | ----------------------------------------------------- |
| Find the component and callers   | `grep`     | Search the prop name, not just the component name     |
| Discover related files           | `glob`     | Locate `*.tsx`, story files, test files               |
| Read the component               | `file_read`| Read before editing; do not edit blind                |
| Refactor                         | `file_edit`| Surgical edits; replace the old API, then the callers |
| Lint and format                  | `lint_format` | `file_paths` array for a multi-file refactor       |
| Confirm the UI still renders     | `browser`  | Exercise every variant; absent ones fail silently     |

## Best Practices

- Composition points are cheap to add and expensive to remove. Add them while the component is small.
- Prefer compound components over a single component with many slots: subcomponents carry their own defaults and keep the parent readable.
- A provider should own state and expose actions. Never expose the raw setter.
- Keep the context interface stable. Adding fields is fine; removing or renaming them is a breaking change.
- Extract a compound component only when two or more call sites actually need the flexibility. Designing for a hypothetical consumer is the same mistake as adding a boolean prop.

## Common Mistakes

- Removing the boolean prop but leaving the behavior gated inside the component, so callers still cannot express the new option.
- Putting state in a subcomponent when it must coordinate a sibling — the subcomponent renders, and the sibling never updates.
- A context interface that exposes setters, so every consumer mutates state directly and the implementation can never change.
- Named compound children written as three exports in one file without a shared namespace, so `<Picker.Panel>` does not exist.
- Refactoring the component but not its callers, leaving dead props that keep the old behavior alive.

## Anti-patterns

- A `variant` switch with eight cases that only differs in class names.
- `renderItem`, `renderHeader`, `renderFooter` props threaded through three levels.
- A god-component accepting 30 props, half of them booleans.
- Reading all seven rule files before touching the code. Read the one that matches.

## Sources

- Rules: `rules/` (seven rule files) at the skill root.
- Compiled guide with every rule expanded: `AGENTS.md` at the skill root.
- Upstream: Vercel Engineering, listed at skills.sh as `vercel-labs/agent-skills/vercel-composition-patterns`.
