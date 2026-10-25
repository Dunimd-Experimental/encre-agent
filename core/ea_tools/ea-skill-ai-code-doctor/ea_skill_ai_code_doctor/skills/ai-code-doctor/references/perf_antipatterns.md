# Python Performance Anti-Patterns

Seven runtime performance anti-patterns for Python. Each entry gives a symptom, the complexity/magnitude, the fix, the expected gain, and a before/after example. Use `scripts/analyze.py` output (complexity scores, I/O/DB call sites) as hard evidence when classifying findings.

When the code is runnable, prefer a **measured** gain (timed before/after). Fall back to an **estimated** gain (theoretical complexity change) only when the code cannot be executed.

---

## 1. ORM N+1 queries

**Symptom**: accessing a relation/foreign-key attribute inside a loop, triggering one extra DB query per iteration.

**Magnitude**: O(n) queries where O(1) would suffice — often the dominant cost in web apps.

**Fix**: use `select_related` (Django / join) or `prefetch_related` (Django / SQLAlchemy / eager-load) to batch into one or two queries.

**Gain**: DB round-trips drop from N+1 to 1–2; wall time often drops 10x–100x for large N.

**Before**:
```python
for user in User.objects.all():        # 1 query
    print(user.profile.bio)            # 1 query EACH → N+1
```

**After**:
```python
for user in User.objects.select_related("profile").all():  # 1 query
    print(user.profile.bio)
```

---

## 2. Reducible complexity

**Symptom**: a double loop (often `for a in xs: for b in xs: if a.id == b.id`) that scans to find matches, where a dict/set index would make lookup O(1).

**Magnitude**: O(n²) → O(n).

**Fix**: build a `dict`/`set` index first, then look up in O(1).

**Gain**: for n=10,000, roughly 50,000,000 comparisons → 10,000 lookups.

**Before**:
```python
def match(orders, items):
    result = []
    for o in orders:
        for it in items:           # O(n*m)
            if it.order_id == o.id:
                result.append((o, it))
    return result
```

**After**:
```python
def match(orders, items):
    by_order = {}
    for it in items:               # O(m)
        by_order.setdefault(it.order_id, []).append(it)
    return [(o, it) for o in orders for it in by_order.get(o.id, [])]  # O(n+m)
```

---

## 3. Serial blocking I/O

**Symptom**: `requests.get(...)` (or any blocking I/O) called inside a loop, each call waiting on the network before the next starts.

**Magnitude**: wall time ≈ sum of all request latencies; should be ≈ max latency.

**Fix**: use `asyncio` + an async client, or `concurrent.futures.ThreadPoolExecutor` for mixed sync code.

**Gain**: wall time Σ→max; for 50 requests × 200ms each, ~10s → ~0.3s.

**Before**:
```python
def fetch_all(urls):
    results = []
    for url in urls:               # serial
        results.append(requests.get(url).json())
    return results
```

**After**:
```python
from concurrent.futures import ThreadPoolExecutor

def fetch_all(urls):
    with ThreadPoolExecutor(max_workers=16) as ex:
        return list(ex.map(lambda u: requests.get(u).json(), urls))
```

---

## 4. Redundant recomputation

**Symptom**: an invariant value (one that does not change across iterations) is recomputed inside the loop body.

**Magnitude**: O(n) wasted recomputations of an O(1) or heavier expression.

**Fix**: hoist the invariant computation out of the loop; cache it in a local.

**Gain**: depends on the recomputed expression's cost; for a regex compile or a function call, often 5x–50x.

**Before**:
```python
def normalize_all(items):
    out = []
    for it in items:
        pattern = re.compile(r"\s+")   # recompiled every iteration
        out.append(pattern.sub(" ", it))
    return out
```

**After**:
```python
def normalize_all(items):
    pattern = re.compile(r"\s+")       # compiled once
    return [pattern.sub(" ", it) for it in items]
```

---

## 5. Inefficient data structure (list for membership)

**Symptom**: using `in` on a `list` for membership tests inside a loop.

**Magnitude**: O(n) per `in` check → O(1) with a `set`.

**Fix**: convert the searched collection to a `set` (or `dict` keys) before the loop.

**Gain**: membership test O(n)→O(1); for a 10,000-element list checked 10,000 times, ~100M comparisons → 10K hashes.

**Before**:
```python
def filter_active(users, active_ids):
    return [u for u in users if u.id in active_ids]   # active_ids is a list
```

**After**:
```python
def filter_active(users, active_ids):
    active = set(active_ids)
    return [u for u in users if u.id in active]
```

---

## 6. String concatenation in loops

**Symptom**: building a string with `s += chunk` inside a loop.

**Magnitude**: O(n²) total time due to repeated immutable copies.

**Fix**: collect parts in a list and `''.join(...)` once.

**Gain**: O(n²)→O(n); for 100,000 chunks, often seconds → milliseconds.

**Before**:
```python
def build_report(lines):
    s = ""
    for line in lines:
        s += line + "\n"      # O(n²)
    return s
```

**After**:
```python
def build_report(lines):
    return "".join(line + "\n" for line in lines)
```

---

## 7. Bulk file load

**Symptom**: `f.read()` reading an entire large file into memory when only a stream or chunked scan is needed.

**Magnitude**: peak memory ≈ file size; should be O(1) or O(chunk).

**Fix**: iterate the file line-by-line (`for line in f:`) or read in fixed-size chunks.

**Gain**: memory peak drops from file-size to constant; enables processing files larger than RAM.

**Before**:
```python
def count_errors(path):
    with open(path) as f:
        text = f.read()        # loads whole file
    return text.count("ERROR")
```

**After**:
```python
def count_errors(path):
    count = 0
    with open(path) as f:
        for line in f:         # streams one line at a time
            count += line.count("ERROR")
    return count
```
