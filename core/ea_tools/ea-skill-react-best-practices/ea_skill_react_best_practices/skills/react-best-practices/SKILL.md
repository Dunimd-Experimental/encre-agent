---
name: react-best-practices
description: "Vercel React and Next.js performance rules. Use when writing, reviewing, or refactoring React or Next.js code: eliminate waterfalls, shrink bundles, speed up server components, deduplicate client fetching, cut rerenders."
aliases:
  - react-perf
  - nextjs-performance
  - "react rules"
argument_hint: Component, route, or bundle to optimize
user_invocable: true
hidden: true
---

# React and Next.js Performance Rules

Seven dozen rules, grouped into eight categories by impact. The categories are an order, not a menu: fix waterfalls and bundle size before you touch a rerender, and treat the JavaScript micro-rules as the last resort, because they only matter after the top two categories are clean.

## When to Apply

- Writing new React or Next.js code.
- Reviewing a pull request for performance regressions.
- Refactoring a route, page, or component that is slow or heavy.
- Diagnosing a slow server component, a large bundle, or unnecessary rerenders.

## When NOT to Use

- Vue or Svelte apps — use the `vue` skill.
- Animation work — use the `gsap-*` skills.
- Visual design review — use `web-design-guidelines` and `frontend-design`.
- General code review outside React — use `code-review`.

## Categories by Priority

| #  | Category                  | Priority | Prefix      | Count |
| -- | ------------------------- | -------- | ----------- | ----- |
| 1  | Eliminating Waterfalls    | CRITICAL | `async-`    | 7     |
| 2  | Bundle Size Optimization  | CRITICAL | `bundle-`   | 6     |
| 3  | Server-Side Performance   | HIGH     | `server-`   | 10    |
| 4  | Client-Side Data Fetching | MEDIUM-HIGH | `client-` | 4    |
| 5  | Re-render Optimization    | MEDIUM   | `rerender-` | 14    |
| 6  | Rendering Performance     | MEDIUM   | `rendering-`| 11    |
| 7  | JavaScript Performance    | LOW-MEDIUM | `js-`     | 14    |
| 8  | Advanced Patterns         | LOW      | `advanced-` | 5     |

## The Rules

### 1. Eliminating Waterfalls (CRITICAL)

- `async-cheap-condition-before-await` — Check a cheap synchronous condition before awaiting flags or remote values.
- `async-defer-await` — Move the await into the branch that actually uses the result.
- `async-parallel` — Await independent operations with `Promise.all()`.
- `async-dependencies` — Use `better-all` so a failed dependency does not sink the whole set.
- `async-api-routes` — Start promises early and await them late.
- `async-suspense-boundaries` — Use Suspense to stream partial results instead of blocking the shell.
- `async-parallel` chains — Do not `await` inside a loop when the items are independent.

### 2. Bundle Size Optimization (CRITICAL)

- `bundle-barrel-imports` — Import from the module directly; barrel files pull the whole index.
- `bundle-analyzable-paths` — Keep import and file-system paths statically analyzable so the bundler can tree-shake.
- `bundle-dynamic-imports` — Use `next/dynamic` for heavy components.
- `bundle-defer-third-party` — Load analytics and logging after hydration.
- `bundle-conditional` — Load a module only when its feature is activated.
- `bundle-preload` — Preload on hover or focus for perceived speed.

### 3. Server-Side Performance (HIGH)

- `server-auth-actions` — Authenticate server actions the way you would API routes.
- `server-cache-react` — Use `React.cache()` to deduplicate per-request work.
- `server-cache-lru` — Use an LRU cache for work that survives across requests.
- `server-dedup-props` — Do not serialize the same data twice through RSC props.
- `server-hoist-static-io` — Hoist static I/O (fonts, logos) to module scope.
- `server-no-shared-module-state` — No mutable module-level request state in RSC or SSR.
- `server-serialization` — Pass the minimum data needed to the client component.
- `server-parallel-fetching` — Restructure components so fetches run in parallel.
- `server-parallel-nested-fetching` — Chain nested fetches and run them in `Promise.all`.
- `server-after-nonblocking` — Use `after()` for work that must not block the response.

### 4. Client-Side Data Fetching (MEDIUM-HIGH)

- `client-swr-dedup` — Use SWR to deduplicate identical in-flight requests.
- `client-event-listeners` — Deduplicate global event listeners across mounts.
- `client-passive-event-listeners` — Use passive listeners for scroll handlers.
- `client-localstorage-schema` — Version and minimize the data you store locally.

### 5. Re-render Optimization (MEDIUM)

- `rerender-defer-reads` — Do not subscribe to state that is only read inside a callback.
- `rerender-memo` — Extract expensive work into memoized components.
- `rerender-memo-with-default-value` — Hoist default non-primitive props out of the render.
- `rerender-dependencies` — Use primitive dependencies in effects.
- `rerender-derived-state` — Subscribe to the derived boolean, not the raw value.
- `rerender-derived-state-no-effect` — Derive state during render instead of in an effect.
- `rerender-functional-setstate` — Use functional `setState` in stable callbacks.
- `rerender-lazy-state-init` — Pass a function to `useState` for expensive values.
- `rerender-simple-expression-in-memo` — Do not memoize simple primitives; memo adds overhead.
- `rerender-split-combined-hooks` — Split hooks that have independent dependencies.
- `rerender-move-effect-to-event` — Put interaction logic in event handlers, not effects.
- `rerender-transitions` — Use `startTransition` for updates that must not block input.
- `rerender-use-deferred-value` — Use `useDeferredValue` for inputs that drive expensive work.
- `rerender-use-ref-transient-values` — Keep transient high-frequency values in a ref.

### 6. Rendering Performance (MEDIUM)

- `rendering-hoist-jsx` — Hoist static JSX out of render bodies.
- `rendering-conditional-render` — Prefer returning early to nested conditionals.
- `rendering-activity` — Use the Activity API instead of nested `useEffect` visibility logic.
- `rendering-content-visibility` — Use `content-visibility: auto` for long lists.
- `rendering-animate-svg-wrapper` — Animate the wrapper of an SVG, not the SVG nodes.
- `rendering-resource-hints` — Emit resource hints for known-critical assets.
- `rendering-script-defer-async` — Mark scripts as `defer` or `async`.
- `rendering-hydration-no-flicker` — Avoid client-only UI that flickers on hydration.
- `rendering-hydration-suppress-warning` — Suppress only the specific hydration mismatch you own.
- `rendering-svg-precision` — Keep SVG path precision low; it is text weight.
- `rendering-usetransition-loading` — Drive loading state from transitions, not booleans.

### 7. JavaScript Performance (LOW-MEDIUM)

- `js-early-exit` — Return as soon as the input is known to be handled.
- `js-length-check-first` — Check length before iterating.
- `js-combine-iterations` — Merge passes over the same collection into one.
- `js-flatmap-filter` — Use `flatMap` instead of `map` then `filter`.
- `js-index-maps` — Build an index map once instead of searching repeatedly.
- `js-set-map-lookups` — Use `Set` or `Map` for membership tests.
- `js-min-max-loop` — Track min and max in one loop.
- `js-cache-function-results` — Memoize pure repeated calls.
- `js-cache-property-access` — Cache repeated property chains behind a local.
- `js-cache-storage` — Cache parsed storage reads.
- `js-hoist-regexp` — Hoist regexps out of hot loops.
- `js-tosorted-immutable` — Use `toSorted` instead of mutating then sorting.
- `js-batch-dom-css` — Batch DOM writes so layout happens once.
- `js-request-idle-callback` — Defer non-urgent work to idle time.

### 8. Advanced Patterns (LOW)

- `advanced-effect-event-deps` — Order effect dependencies correctly relative to event handlers.
- `advanced-event-handler-refs` — Keep event handlers stable via refs.
- `advanced-init-once` — Run one-time initialization exactly once.
- `advanced-use-latest` — Always read the latest value inside async or deferred callbacks.

## Rule Files

The full rule set lives in `rules/` at the skill root: 72 rule files, plus `_sections` and `_template` scaffolding. Each rule file has the why, an incorrect example with an explanation, a correct example with an explanation, and extra context. Read the one or two that match the problem you are solving.

The compiled document with every rule expanded is `AGENTS.md` at the skill root. It is large; read it only when you need several rules at once, and read it in slices.

## How to Apply

1. **Measure before optimizing.** A waterfall or an oversized bundle is measurable; a rerender is not worth chasing until the first two categories are clean.
2. **Locate the candidate with `grep`.** Slow patterns are findable: `await` inside a `for`, `import` from an index file, `JSON.parse(localStorage`, `useEffect` with an object dependency.
3. **Read the matching rule file** for the exact fix, then apply it with `file_edit`.
4. **Verify.** Run `lint_format` on the touched files and check the route still renders; for rendering or hydration rules, load the page in a real browser and look at the console.

## Tools You Use

| Task                          | Tool         | Note                                            |
| ----------------------------- | ------------ | ----------------------------------------------- |
| Find the slow pattern         | `grep`       | Search for `await`, import paths, effect deps   |
| Discover the files            | `glob`       | Scope the search before reading                 |
| Read the component            | `file_read`  | Read before editing                            |
| Apply the fix                 | `file_edit`  | One rule per edit keeps the diff reviewable    |
| Lint and format               | `lint_format`| `file_paths` accepts a list                    |
| Confirm hydration and console | `browser`    | Hydration errors and warnings show up here     |

## Best Practices

- Fix in category order. A waterfall fixed beats four memoizations.
- Do not reach for `js-*` rules on code that is not actually hot. Premature micro-optimization adds complexity with no gain.
- Prefer restructuring (parallel fetching, Suspense boundaries) over wrapping (memo, useCallback) whenever the structure allows it.
- Keep the diff to one rule at a time so a review can reject one rule without rejecting the rest.

## Common Mistakes

- Adding `useMemo` and `React.memo` to everything instead of finding the waterfall.
- Waiting on each fetch inside a loop, which turns a parallel page into a serial one.
- Importing from a barrel file to save one line, then paying for the whole module.
- Applying a `js-*` rule to a rarely executed path.
- Reading all 72 rule files or the whole compiled document before writing any code.

## Anti-patterns

- Optimizing a component that renders once.
- Memoizing a component whose props change every render.
- Caching across requests with module-level mutable state in RSC.
- Suppressing a hydration warning without fixing the mismatch.

## Sources

- Rules: 72 files in `rules/` at the skill root.
- Compiled guide: `AGENTS.md` at the skill root.
- Upstream: Vercel Engineering, listed at skills.sh as `vercel-labs/agent-skills/vercel-react-best-practices`.
