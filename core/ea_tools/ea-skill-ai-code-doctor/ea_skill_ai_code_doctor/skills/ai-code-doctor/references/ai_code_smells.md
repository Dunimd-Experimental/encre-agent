# AI Code Smells Catalog

Six smells that AI-generated Python code tends to exhibit. Rule-based linters catch few of these — they require semantic understanding to detect and to fix safely. Each entry below gives a definition, why AI-generated code tends to produce it, detection features, and a before/after example.

---

## 1. Over-defensive coding

**Definition**: excessive guarding against states that cannot occur, or exception handling that swallows errors silently.

**Why AI produces it**: LLMs are trained to hedge against every possible failure, producing try/except around trivial code and None checks on values that are obviously non-null from context.

**Detection features**:
- `try/except` with bare `except:` or `except Exception:` that only `pass` / `continue`.
- `except` blocks that swallow the exception without logging or re-raising.
- `if x is None` immediately after `x = []` / `x = {}` / `x = func()` where `func` never returns None.
- Repeated null checks on the same variable within one function.
- Nested try/except where the inner one adds no value.

**Before**:
```python
def get_user_name(user):
    try:
        try:
            name = user.name
        except:
            name = None
        if name is not None:
            if name is not None:
                return name
        return None
    except:
        return None
```

**After**:
```python
def get_user_name(user):
    return user.name
```

---

## 2. Verbose boilerplate / filler comments

**Definition**: comments that restate the code in words, or docstrings that add no information beyond the signature.

**Why AI produces it**: LLMs are trained to "document everything," producing comments that translate each line into prose and docstrings that paraphrase the parameter names.

**Detection features**:
- Comments like `# set x to 0` above `x = 0`, `# loop over items` above `for item in items`.
- Docstrings that only repeat the function name and parameter names with no semantics.
- Comments that describe *what* the code does (already obvious) rather than *why*.

**Before**:
```python
def calculate_total(items):
    # Initialize total to 0
    total = 0
    # Loop over each item in items
    for item in items:
        # Add the item price to total
        total = total + item.price
    # Return the total
    return total
```

**After**:
```python
def calculate_total(items):
    return sum(item.price for item in items)
```

---

## 3. Copy-paste patterns

**Definition**: multiple functions or blocks with near-identical structure that differ only in data, which should be parameterized or extracted into one.

**Why AI produces it**: LLMs complete each request fresh and often regenerate a similar solution rather than generalizing, especially when asked for several related operations in one turn.

**Detection features**:
- 2+ functions whose AST shape is nearly identical and differ only in constants or field names.
- Repeated try/except/finally scaffolding around different one-line bodies.
- A sequence of `if x == "a": ... elif x == "b": ...` branches that all do the same kind of work.

**Before**:
```python
def get_user_email(user):
    try:
        return user.email
    except:
        return None

def get_user_phone(user):
    try:
        return user.phone
    except:
        return None

def get_user_address(user):
    try:
        return user.address
    except:
        return None
```

**After**:
```python
def get_user_field(user, field, default=None):
    return getattr(user, field, default)
```

---

## 4. Over-engineering

**Definition**: abstraction layers, factories, or interface points that serve only a single concrete case and exist for extensions that will never come.

**Why AI produces it**: LLMs have seen a lot of enterprise patterns in training data and apply them by default, "future-proofing" code that does not need it.

**Detection features**:
- A factory class with one concrete product.
- An abstract base class with one subclass.
- A config/settings layer indirection for values used in exactly one place.
- Interfaces reserved for "future implementations" with no current second implementation.

**Before**:
```python
class GreeterFactory:
    def create_greeter(self, lang):
        if lang == "en":
            return EnglishGreeter()
        return EnglishGreeter()

class EnglishGreeter:
    def greet(self):
        return "Hello"
```

**After**:
```python
def greet():
    return "Hello"
```

---

## 5. Hallucinated API calls

**Definition**: calls to methods, parameters, or modules that do not exist, or correct names used with the wrong signature.

**Why AI produces it**: LLMs blend APIs seen in training and can emit plausible-looking but non-existent calls, especially with libraries that have similar names across versions.

**Detection features**:
- Method names that look right but are not in the class (e.g., `list.push`, `str.slice`).
- Import of a module that does not exist or a submodule name that is wrong.
- Keyword arguments that the function does not accept.
- Use of a function from the wrong module (e.g., `pandas.read_json` called as `json.read`).

**Note**: Detecting this reliably may require running or type-checking the code. When the code cannot be run, flag suspected hallucinations as "verify this call exists" rather than asserting they are wrong.

**Before**:
```python
import pandas
df = pandas.DataFrame(data)
df.push(new_row)          # does not exist; should be concat/append
trimmed = df.slice(0, 10) # does not exist; should be iloc/loc
```

**After**:
```python
import pandas as pd
df = pd.DataFrame(data)
df = pd.concat([df, new_row])
trimmed = df.iloc[:10]
```

---

## 6. Style drift

**Definition**: inconsistent naming or construction style within a single file or module, where the same kind of operation is done two or three different ways.

**Why AI produces it**: LLMs blend many training sources and may switch styles mid-file, especially in long generations or when the prompt itself is inconsistent.

**Detection features**:
- Mixed `snake_case` and `camelCase` for function/variable names in one file.
- Mixed string formatting: `f"{x}"`, `"%s" % x`, `"{}".format(x)` in the same file.
- Mixed collection construction: `dict(a=1)` and `{"a": 1}` interchangeably.
- Mixed import styles: `from x import y` and `import x.y` for the same module.

**Before**:
```python
def getUserName(user):
    return f"{user.firstName} {user.lastName}"

def get_user_email(user):
    return "{}".format(user.email)

def GetUserPhone(user):
    return "%s" % user.phone
```

**After**:
```python
def get_user_name(user):
    return f"{user.first_name} {user.last_name}"

def get_user_email(user):
    return user.email

def get_user_phone(user):
    return user.phone
```
