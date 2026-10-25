---
name: vue
description: Build Vue 3 applications with Composition API, correct reactivity patterns, and production-ready components. Use for Vue component design, composables, state management, and performance tuning.
aliases:
  - vue3
  - "vue 3"
  - composition-api
  - vueuse
argument_hint: What are you building or debugging?
user_invocable: true
hidden: true
---

# Vue 3

Build Vue 3 applications correctly: composition-first structure, reactivity that actually tracks, and performance that does not degrade as the app grows.

Read the full reference set before deep work: `reactivity.md` (ref vs reactive, shallow, readonly, raw), `components.md` (props, emits, slots, dynamic/async components, Teleport), `composables.md` (designing reusable logic), `performance.md` (render, list, build).

## When to Use

- New Vue 3 component, composable, or feature module.
- A reactive state does not update (or updates too much).
- Refactoring Options API to Composition API.
- Performance tuning: re-renders, large lists, bundle size.
- State management decisions (local ref vs store vs provide/inject).

## When NOT to Use

- React or Solid work -> use those frameworks' own skills.
- Generic JavaScript/TypeScript questions -> plain code review.
- Vue 2 codebase -> reactivity rules differ materially; check before applying Vue 3 idioms.

## Composition API Philosophy

- Composition API is about better code organization, not replacing Options API.
- Group code by feature, not by option type - related logic stays together.
- Extract reusable logic into composables; that is the main win of Composition API.
- `<script setup>` is the recommended syntax: cleaner and better compiled.

## Reference Files

| Topic | File |
|-------|------|
| Reactivity patterns (ref vs reactive, shallow, readonly, raw, effect cleanup) | `reactivity.md` |
| Component patterns (props, emits, slots, dynamic, async, Teleport) | `components.md` |
| Composable design (naming, inputs, returns, lifecycle, async) | `composables.md` |
| Performance (renders, lists, memoization, build) | `performance.md` |

## Reactivity Traps

- `ref` for primitives - access with `.value` in script, auto-unwrapped in template.
- `reactive` cannot be reassigned as a whole object - `state = {...}` breaks reactivity.
- Destructuring a `reactive` object loses reactivity - use `toRefs(state)`.
- Nested refs unwrap inside `reactive` - `reactive({count: ref(0)}).count` is a number, not a ref.
- `computed` is for derived state: pure, cached, synchronous. No side effects, no async.
- `watch` is for side effects: it receives `(newVal, oldVal)`. `watchEffect` auto-tracks but gives no old value.
- `immediate: true` for the initial run; `deep: true` (or a getter) when watching object interiors.
- `readonly` guards state exposed to a composable consumer; props are already readonly.

## Watch vs Computed

```ts
// derived state: cached, pure
const fullName = computed(() => `${first.value} ${last.value}`)

// side effect: fetch, log, scroll, persist
watch(() => route.query.id, async (id) => {
  if (!id) return
  data.value = await api.get(id)
}, { immediate: true })

// auto-tracked, with cleanup
watchEffect((onCleanup) => {
  const ctrl = new AbortController()
  load(ctrl.signal)
  onCleanup(() => ctrl.abort())
})
```

## Props, Emits, v-model

- `defineProps<{ msg: string; count?: number }>()` - type-safe; required props have no `?`.
- Defaults: `withDefaults(defineProps<Props>(), { count: 0 })`. Default for objects must be a factory.
- Props are readonly - never mutate, emit an event instead.
- `defineEmits<{ (e: 'update', value: string): void }>()` - emit returns false if a handler returned false.
- `v-model` is `:modelValue` + `@update:modelValue`; custom `defineModel()` in 3.4+.

## Composables

- `use` prefix: `useMouse`, `useAuth`, `useFetch`.
- Accept a ref OR a plain value; `toValue()` (3.3+) unwraps either.
- Return an object of named refs plus methods - refs destructure cleanly; `reactive` does not.
- `onMounted`/`onUnmounted` work inside a composable, bound to the calling component.
- Must be called synchronously in `setup` - not inside callbacks, conditionals, or async.

```ts
export function useCounter(initial = 0) {
  const count = ref(initial)
  return { count, increment: () => count.value++, decrement: () => count.value-- }
}
```

## Lifecycle Traps

- `onMounted` for DOM access; `onUnmounted` for cleanup (subscriptions, timers, listeners).
- Lifecycle hooks must be called synchronously in `setup`.
- Async `setup` needs `<Suspense>`.
- A `ref="name"` template ref requires `const name = ref(null)`, matching exactly.

## Router Traps

- `useRoute()` is reactive and read in `setup`; `useRouter()` for `router.push('/path')`.
- Guards: `beforeEach`, `beforeResolve`, `afterEach`; return `false` to cancel.
- Named views via `<RouterView>` for multiple outlets per route.

## Performance

- Stable unique `:key` on every `v-for` - never array index in a reorderable list.
- `v-if` unmounts; `v-show` toggles CSS - `v-show` wins for frequent toggles, `v-if` for conditional mounts.
- `v-once` for static content; `v-memo="[dep]"` to memoize a fragment.
- Virtual scroll (`vue-virtual-scroller`) or pagination past ~1000 rows.
- `shallowRef` for large objects you never mutate deeply; `computed`/`toRef` for stable derived props.
- `defineAsyncComponent(() => import('./Comp.vue'))` plus route-level splitting for bundle size.
- `vite build` tree-shakes; `vite-plugin-visualizer` finds the heavy dependencies.

## Common Mistakes

- `v-if` vs `v-show` swapped for frequent toggles - full unmount each flip.
- `:key` from array index - broken transitions and recycled state.
- Mutating a prop directly - silent, unrecoverable drift.
- Reassigning a `reactive` object - reactivity lost forever.
- Destructuring a `reactive` object - flat snapshot, no tracking.
- `watch` without `deep: true` or a getter - object changes never observed.
- Lifecycle hook called inside a callback - the warning is correct; restructure.
- Skipping `<Teleport to="body">` for modals trapped inside an overflow container.

## Best Practices

1. `<script setup>` + TypeScript generics for props and emits.
2. Composables for anything stateful and reused twice.
3. Local `ref` first; move to a store only when three+ unrelated components need it.
4. One component = one responsibility; extract when the template exceeds ~80 lines.
5. Verify with `npm run build` and a dev-server smoke check after changes.

## Anti-patterns

- Options API and Composition API mixed inside one component.
- `reactive` for a large, deeply-nested object that rarely mutates.
- Computed chains with side effects, "because it feels simpler".
- `v-for` without `:key`, or keys that change identity on every render.
- Direct `store.state.x = ...` writes from components.
- Reaching for a third-party state library before provide/inject is proven insufficient.
