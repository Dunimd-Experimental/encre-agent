# Refactor Playbook

Refactor patterns used when prescribing fixes in the diagnostic report. Each pattern states when to apply it, the transformation, and a caution. All patterns here preserve observable behavior — they change structure, not logic.

---

## 1. Extract function

**When**: a function is too long, mixes several responsibilities, or has a fragment that is reused elsewhere.

**Transformation**: move a cohesive block into a named function with parameters for the values it depends on; replace the block with a call.

**Caution**: do not extract fragments that mutate many outer locals — passing all of them as parameters defeats the purpose.

```python
# Before
def process_order(order):
    # validate
    if not order.items:
        raise ValueError("empty")
    if order.total < 0:
        raise ValueError("negative")
    # compute
    tax = order.total * 0.1
    # ... 40 more lines

# After
def validate_order(order):
    if not order.items:
        raise ValueError("empty")
    if order.total < 0:
        raise ValueError("negative")

def process_order(order):
    validate_order(order)
    tax = order.total * 0.1
    # ...
```

---

## 2. Replace if-elif chain with dict dispatch

**When**: a chain of `if x == "a": ... elif x == "b": ...` where each branch does the same *kind* of work.

**Transformation**: map the discriminator to a handler (function or value) in a dict; look up and call.

**Caution**: only when branches are truly parallel. If branches have side effects on shared state with ordering dependencies, keep the chain.

```python
# Before
def handle(event):
    if event.type == "click":
        do_click(event)
    elif event.type == "hover":
        do_hover(event)
    elif event.type == "submit":
        do_submit(event)
    else:
        do_default(event)

# After
HANDLERS = {
    "click": do_click,
    "hover": do_hover,
    "submit": do_submit,
}
def handle(event):
    HANDLERS.get(event.type, do_default)(event)
```

---

## 3. Early return to reduce nesting

**When**: a function has a deep `if/else` pyramid where the `else` is the main path and the `if` is a guard.

**Transformation**: invert the guard and return early; the main path sits at the top indentation level.

```python
# Before
def get_discount(user):
    if user is not None:
        if user.is_member:
            if user.years > 5:
                return 0.2
            else:
                return 0.1
        else:
            return 0.0
    return 0.0

# After
def get_discount(user):
    if user is None:
        return 0.0
    if not user.is_member:
        return 0.0
    return 0.2 if user.years > 5 else 0.1
```

---

## 4. Generator over materialized list

**When**: a list is built only to be iterated once, especially when it may be large.

**Transformation**: replace the list with a generator expression or a `yield` function.

**Caution**: generators are single-pass; if the consumer iterates twice or needs `len()`, keep the list.

```python
# Before
def squares(n):
    result = []
    for i in range(n):
        result.append(i * i)
    return result
total = sum(squares(10_000_000))   # builds a 10M list

# After
def squares(n):
    for i in range(n):
        yield i * i
total = sum(squares(10_000_000))   # constant memory
```

---

## 5. Comprehension over loop-append

**When**: a loop that only appends transformed/filtered items to a list.

**Transformation**: replace with a list/dict/set comprehension.

```python
# Before
evens = []
for x in numbers:
    if x % 2 == 0:
        evens.append(x * 2)

# After
evens = [x * 2 for x in numbers if x % 2 == 0]
```

---

## 6. Context manager for resources

**When**: a file/socket/lock is opened and must be closed, often scattered across try/finally.

**Transformation**: use `with` to guarantee cleanup; remove the manual close/finally.

```python
# Before
f = open(path)
try:
    data = f.read()
finally:
    f.close()

# After
with open(path) as f:
    data = f.read()
```

---

## 7. Replace repeated try/except with a helper

**When**: the same try/except scaffolding wraps many different one-line bodies (common in AI-generated code).

**Transformation**: extract a helper that takes the operation as a callable and handles the exception once.

```python
# Before
def get_a(o):
    try: return o.a
    except: return None
def get_b(o):
    try: return o.b
    except: return None

# After
def safe_get(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default
```

---

## 8. Strategy pattern (only when there are 2+ real strategies)

**When**: behavior must vary by a discriminator and each variant is non-trivial; AND there are at least two real implementations today (not a single stub).

**Caution**: do not introduce this for a single concrete case — that is over-engineering (smell #4). See `ai_code_smells.md` section 4.

```python
# Only when SortStrategy has 2+ real implementations
class SortStrategy:
    def sort(self, data): raise NotImplementedError
class QuickSort(SortStrategy): ...
class MergeSort(SortStrategy): ...
```
