---
name: react-view-transitions
description: Animate UI state changes with React's View Transition API - the ViewTransition component, addTransitionType, CSS view transition pseudo-elements, shared element morphs, Suspense reveals, list reorder, and Next.js integration.
aliases:
  - view-transitions
  - startViewTransition
  - react-animation
argument_hint: Which navigation or state change needs animating?
user_invocable: true
hidden: true
---

# React View Transitions

Animate between UI states with the browser's native `document.startViewTransition`. Declare *what* with `<ViewTransition>`, trigger *when* with `startTransition` / `useDeferredValue` / `Suspense`, control *how* with CSS classes. Unsupported browsers skip animations gracefully.

**Read before implementing:** follow `references/implementation.md` step by step (start with the audit - do not skip it), and copy the CSS from `references/css-recipes.md` verbatim. Do not write your own animation CSS. Full compiled guide: `AGENTS.md`.

## When to Use

- Page/route transitions, shared-element morphs, enter/exit of components.
- List reorder, directional forward/back navigation.
- Next.js App Router transitions or Suspense reveals.
- "Smooth native animation without an animation library".

## When NOT to Use

- Complex choreography, physics, scroll-driven animation -> GSAP skills.
- CSS-only micro-interactions that never cross a transition boundary.
- Vue or non-React apps.

## Availability

- **Next.js:** do **not** install `react@canary` - the App Router bundles React canary internally. `ViewTransition` works out of the box; `npm ls react` showing a stable version is expected.
- **Without Next.js:** install `react@canary react-dom@canary` (`ViewTransition` is not in stable React).
- Browsers: Chromium 111+, Firefox 144+, Safari 18.2+. Graceful degradation.

## Tools You Use

| Task | Tool |
|------|------|
| Find links, routers, Suspense boundaries | `grep`, `glob` |
| Read/edit components and stylesheets | `file_read`, `file_write` |
| Lint and typecheck after changes | `lint_format` |
| Drive the dev server and verify each path | `terminal`, `browser` |

## When to Animate

Every `<ViewTransition>` must communicate a spatial relationship or continuity. If you cannot articulate what it communicates, do not add it.

Implement **all** applicable patterns, in this order:

| Priority | Pattern | Communicates |
|----------|---------|--------------|
| 1 | Shared element (`name`) | "Same thing - going deeper" |
| 2 | Suspense reveal | "Data loaded" |
| 3 | List identity (per-item `key`) | "Same items, new arrangement" |
| 4 | State change (`enter`/`exit`) | "Something appeared/disappeared" |
| 5 | Route change (layout-level) | "Going to a new place" |

This is an implementation order, not a pick-one list. Skip a pattern only when the app has no use case for it.

| Context | Animation | Why |
|---------|-----------|-----|
| Hierarchical (list -> detail) | type-keyed `nav-forward` / `nav-back` | communicates spatial depth |
| Lateral (tab to tab) | bare `<ViewTransition>` fade or `default="none"` | no depth to communicate |
| Suspense reveal | `enter`/`exit` string props | content arriving |
| Background revalidation | `default="none"` | silent, nothing to show |

Reserve directional slides for hierarchical navigation and ordered sequences (prev/next, carousel, paginated results) - there direction means position. Never use directional slides for lateral, unordered navigation.

## Core Concepts

```jsx
import { ViewTransition } from 'react'

<ViewTransition enter="auto" exit="auto">
  <Component />
</ViewTransition>
```

React assigns a unique `view-transition-name` and calls `document.startViewTransition` itself. Never call `startViewTransition` from app code.

| Trigger | Fires when |
|---------|-----------|
| `enter` | VT first inserted during a Transition |
| `exit` | VT first removed during a Transition |
| `update` | DOM mutations inside a VT (innermost VT in a nested pair) |
| `share` | a named VT unmounts and another with the same `name` mounts in the same Transition |

Only `startTransition`, `useDeferredValue`, and `Suspense` activate VTs. A plain `setState` does not animate.

**Critical placement rule** - `enter`/`exit` activate only when the VT appears before any DOM nodes:

```jsx
// Works
<ViewTransition enter="auto" exit="auto"><div>Content</div></ViewTransition>

// Broken - the div wraps the VT, suppressing enter/exit
<div><ViewTransition enter="auto" exit="auto"><div>Content</div></ViewTransition></div>
```

## Styling

Prop values: `"auto"` (browser cross-fade), `"none"` (off), `"class-name"`, or `{ [type]: value }` for type-specific animations.

```jsx
<ViewTransition default="none" enter="slide-in" exit="slide-out" share="morph" />
```

With `default="none"`, every trigger is off unless explicitly listed.

Pseudo-elements: `::view-transition-old(.class)`, `::view-transition-new(.class)`, `::view-transition-group(.class)`, `::view-transition-image-pair(.class)`.

## Transition Types

Tag the update with `addTransitionType`; call it multiple times to stack - different VTs in the tree react to different types.

```jsx
startTransition(() => {
  addTransitionType('nav-forward')
  router.push('/detail/1')
})
```

Map types to classes on `enter`, `exit`, **and** `share`:

```jsx
<ViewTransition
  enter={{ 'nav-forward': 'slide-from-right', 'nav-back': 'slide-from-left', default: 'none' }}
  exit={{ 'nav-forward': 'slide-to-left', 'nav-back': 'slide-to-right', default: 'none' }}
  share={{ 'nav-forward': 'morph-forward', 'nav-back': 'morph-back', default: 'morph' }}
  default="none"
>
  <Page />
</ViewTransition>
```

`enter` and `exit` need not be symmetric (fade in, directional slide out). TypeScript's `ViewTransitionClassPerType` requires the `default` key in every object. Extract the type-keyed VT into a `DirectionalTransition` wrapper so pages do not repeat the map.

- `router.back()` and the browser back/forward buttons do **not** fire view transitions (`popstate` is synchronous). Use `router.push()` with an explicit URL.
- Types are available during navigation but **not** on later Suspense reveals. Use type maps for page-level enter/exit; use plain strings for Suspense reveals.

## Shared Element Transitions

Same `name` on two VTs - one unmounting, one mounting - morphs between them.

```jsx
<ViewTransition name="hero-image">
  <img src="/thumb.jpg" onClick={() => startTransition(() => onSelect())} />
</ViewTransition>

// other view, same name
<ViewTransition name="hero-image">
  <img src="/full.jpg" />
</ViewTransition>
```

- Only one mounted VT per `name`. Use unique, ID-derived names (`photo-${id}`). If a component with a named VT renders in both a modal and a page, both mount at once and the morph breaks - make the name conditional via a prop, or move the named VT into the specific consumer.
- `share` takes precedence over `enter`/`exit`. For every navigation path, check whether a pair actually forms; otherwise `enter`/`exit` fires and the element needs a fallback animation.
- Never pair a shared morph with a fade-out exit - use a directional slide.

## Common Patterns

Enter/exit for conditionally mounted UI:

```jsx
{show && (
  <ViewTransition enter="fade-in" exit="fade-out"><Panel /></ViewTransition>
)}
```

List reorder - one VT per item, stable `key`, no wrapper `<div>` between list and VT, triggered inside `startTransition`.

Shared element inside a list item needs **two nested VT boundaries** - list identity and shared element are independent concerns:

```jsx
{items.map(item => (
  <ViewTransition key={item.id}>                                    {/* list identity */}
    <Link href={`/items/${item.id}`}>
      <ViewTransition name={`item-image-${item.id}`} share="morph"> {/* shared element */}
        <Image src={item.image} />
      </ViewTransition>
      <p>{item.name}</p>
    </Link>
  </ViewTransition>
))}
```

Missing either layer means that animation silently never happens.

Force re-enter with `key={searchParams.toString()}` - but note that keying a `<Suspense>` wrapper remounts the boundary and refetches.

```jsx
<ViewTransition key={searchParams.toString()} enter="slide-up" default="none">
  <ResultsGrid />
</ViewTransition>
```

## How Multiple VTs Interact

All VTs matching the trigger fire in a single `document.startViewTransition`; VTs in different transitions (navigation vs a later Suspense resolve) do not compete.

- **Use `default="none"` liberally.** Without it, every VT fires a browser cross-fade on every transition - Suspense resolves, `useDeferredValue` updates, background revalidation.
- **Two patterns coexist.** Directional type-keyed VTs on page components (fire during navigation) and simple string-prop VTs for Suspense reveals (fire when data lands). `default="none"` on both prevents cross-interference. Always pair `enter` with `exit`. Put directional VTs in page components, never layouts.
- **Nested VT limitation.** When a parent VT exits, nested VTs do not fire their own enter/exit - only the outermost animates. Per-item staggered page navigation is not possible today. A layout VT wrapping `{children}` silently kills page-level enter/exit; remove it.

## Next.js Integration

- `experimental: { viewTransition: true }` in `next.config.js`. Do not install `react@canary`.
- `<Link transitionTypes={['nav-forward']}>` works in Server Components (Next 16+, or 15+ canary). Check with `grep -r "transitionTypes" node_modules/next/dist/`. When unavailable, use `startTransition` + `addTransitionType` + `router.push()`.
- `loading.tsx` is an implicit `<Suspense>` boundary: wrap the skeleton in `<ViewTransition exit="...">`, the content in `<ViewTransition enter="..." default="none">`.
- Server-side search/sort/filter: `startTransition` + `router.replace`; items in `<ViewTransition key={item.id}>` animate reorder.
- Same-route dynamic segments: the page stays mounted so enter/exit never fire - use `key={slug}` + `name` + `share`.
- `<ViewTransition>` works in both Server and Client Components; `addTransitionType` and programmatic navigation require a Client Component.

## Accessibility

Always ship the reduced-motion block from `references/css-recipes.md`:

```css
@media (prefers-reduced-motion: reduce) {
  ::view-transition-old(*),
  ::view-transition-new(*),
  ::view-transition-group(*) {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
  }
}
```

## Common Mistakes

- Bare VT without `default="none"` - cross-fades on every single transition.
- Directional VT in a layout - layouts persist, so enter/exit never fires on route change.
- Fade-out exit alongside a shared morph - conflicts with the morph.
- Writing custom animation CSS instead of the recipes.
- Omitting `default: "none"` in a type-keyed object - TypeScript errors, and the fallback is `"auto"`.
- Type maps on Suspense reveals - those transitions carry no type.
- Raw `view-transition-name` CSS used to *trigger* animation - React only starts transitions when `<ViewTransition>` components are in the tree; bare names isolate elements.
- `update` trigger for same-route navigation - nested VTs steal the mutation; use `key` + `name` + `share`.
- Named VT in a reusable component rendered in both a modal and a page.
- `router.back()` for back navigation - synchronous `popstate` skips animation.
- `border-radius` lost on captured elements - apply it directly to the element.

## Best Practices

1. Audit first: every `<Link>` and `router.push`, every `<Suspense>`, every route, every persistent element, every skeleton/content control pair. Produce a navigation map.
2. Copy the complete CSS recipe set into the global stylesheet.
3. Isolate persistent elements with an inline `viewTransitionName` plus `::view-transition-group(...) { animation: none; z-index: 100 }`; use the backdrop-blur workaround for blurred navbars.
4. Timing budget: direct toggle 100-200ms, route 150-250ms, Suspense reveal 200-400ms, shared morph 300-500ms.
5. Walk every row of the navigation map in a real browser before calling it done.

## Sources

- `references/implementation.md` - step-by-step workflow, audit, verification.
- `references/patterns.md` - patterns, timing, events API, troubleshooting.
- `references/css-recipes.md` - copy-paste CSS.
- `references/nextjs.md` - App Router and Server Component details.
- `AGENTS.md` - full compiled document (Vercel Engineering, v1.0.0).
