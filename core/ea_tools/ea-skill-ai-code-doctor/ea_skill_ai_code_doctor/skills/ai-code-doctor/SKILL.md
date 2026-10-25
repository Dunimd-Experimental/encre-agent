---
name: ai-code-doctor
description: This skill performs a two-layer quality and performance audit on Python code, with special focus on code that was written or assisted by AI. Use it whenever a user wants Python code checked, reviewed, improved, or optimized—whether they point at specific code or simply hand over a file or project and ask the assistant to find issues on its own. The skill first scans for AI-specific code smells (over-defensive coding, verbose boilerplate, copy-paste patterns, over-engineering, hallucinated APIs, style drift), then runs a performance deep-check on hot paths (complexity reduction, ORM N+1 queries, blocking I/O, redundant computation, inefficient data structures, string concatenation in loops), using a static-analysis script for hard evidence. It outputs optimized code plus a diagnostic report explaining each problem, its impact, the proposed fix, and expected performance gain. It covers both pinpoint audits (user names the location) and full self-audits (user does not know where the problems are and lets the assistant locate them). Do not trigger for non-Python code or for tasks unrelated to code review and optimization.
---

# AI Code Doctor

A two-layer Python code auditor. Layer one detects AI-specific code smells. Layer two checks runtime performance on hot paths. Outputs optimized code plus a self-contained diagnostic report. Apply changes to the original file only after the user explicitly confirms.

## Overview

This skill turns a general assistant into a focused Python code doctor that diagnoses AI-written and hand-written code, then prescribes safe, behavior-preserving optimizations. The differentiator is the two-layer audit: layer one ("AI smell self-audit") catches patterns unique to AI-generated code that rule-based linters miss; layer two ("performance deep-check") backs its claims with static-analysis data from `scripts/analyze.py` rather than guesswork.

## Workflow

Run the six steps in order. Step 2 (recon) executes only in full-self-audit mode.

### Step 1 — Receive

Accept Python code from the user. Two input modes:

- **Pinpoint mode**: the user points at a specific file, function, or line range. Skip Step 2 and audit exactly what was named.
- **Full self-audit mode**: the user hands over a file or project without specifying where the problems are. Enter Step 2 to locate hotspots first.

Detect the mode from the user's request: if a location is named → pinpoint; if the request is "review / check / optimize this project or file" without a location → full self-audit.

### Step 2 — Recon (full self-audit mode only)

Locate candidate hotspots before auditing, so the audit is methodical rather than a blind scan.

1. Run `python scripts/analyze.py <file_or_dir>` and read the JSON output. Project scans skip dependency, VCS, and cache directories such as `.venv`, `site-packages`, `node_modules`, `.git`, and `__pycache__`.
2. Rank candidates by risk: high cyclomatic complexity > on an I/O or DB call path > hub function (called from multiple places) > ordinary function.
3. Select the top N (default 5–8) as the audit target list.
4. In pinpoint mode, skip this step entirely.

### Step 3 — Layer one: AI smell self-audit (primary)

Scan the target code for the six AI-specific smells. Load `references/ai_code_smells.md` for the full catalog with detection features and before/after examples. For each smell found, record: location (function + line), the offending snippet, why it is a smell, and the proposed fix.

The six smells:

1. **Over-defensive coding** — excessive try/except that swallows exceptions, redundant None checks on obviously-non-null values, guarding impossible states.
2. **Verbose boilerplate / filler comments** — comments that restate the code in words ("set x to 0"), over-long docstrings with no information gain.
3. **Copy-paste patterns** — multiple functions with near-identical structure that should be parameterized or extracted into one.
4. **Over-engineering** — single-use factories/abstract classes, interface points reserved for extensions that do not exist, needless config layers.
5. **Hallucinated API calls** — methods/parameter names that do not exist, imports of non-existent modules, wrong call signatures.
6. **Style drift** — mixed snake_case/camelCase in one file, mixed f-string/%/.format, inconsistent construction styles.

### Step 4 — Layer two: performance deep-check (secondary)

Check the target code against Python performance anti-patterns. Load `references/perf_antipatterns.md` for the full catalog with symptoms, magnitude, fix, and expected gain. Use `scripts/analyze.py` output (complexity scores, I/O/DB call sites) as hard evidence rather than asserting from intuition.

The seven anti-patterns:

1. **ORM N+1 queries** — accessing relation fields inside a loop (SQLAlchemy/Django ORM).
2. **Reducible complexity** — O(n²) double loops that can drop to O(n) with a set/dict index.
3. **Serial blocking I/O** — `requests.get` (or similar) inside a loop; should be async or concurrent.
4. **Redundant recomputation** — recomputing an invariant value inside a loop.
5. **Inefficient data structure** — using a list for `in` membership tests (should be a set).
6. **String concatenation in loops** — `s += x` in a loop (should be `''.join(list)`).
7. **Bulk file load** — `f.read()` on a large file (should be streaming/chunked).

For each finding, classify the gain as **measured** (when the code is runnable and a timed before/after comparison is feasible) or **estimated** (theoretical, e.g., O(n²)→O(n), when the code cannot be run). Prefer measured gains; fall back to estimated only when running is impractical.

### Step 5 — Synthesize diagnostic report

Merge layer-one and layer-two findings into one report. Sort by severity: crash/correctness risk > performance > readability. For each finding, write the four-part entry:

- **Problem**: what is wrong, where (function + line).
- **Impact**: what harm it causes (perf cost, maintainability, bug risk).
- **Fix**: the proposed refactor or rewrite.
- **Gain**: measured timing improvement or estimated complexity change, tagged ④ (smell) or ③ (perf).

Compute an overall health score (0–100): start at 100, deduct per finding by severity.

Before calling the optimized code runnable, compare its interface with the source:
`python scripts/check_interface.py <original.py> <optimized.py>`. Treat missing or changed
functions, classes, methods, or parameter signatures as a blocking finding. Preserve
compatibility with wrappers when an internal helper extraction is needed.

Run `python -m py_compile <optimized.py>` and an import smoke test when dependencies are
available. If an import fails, label the output as blocked and list the missing package or
runtime prerequisite; never claim that the artifact is runnable without this evidence.

### Step 6 — Prescribe (output)

Produce the output package:

1. **Chat layer (immediate)**: one-line health verdict, top 3 findings, pointers to the artifact files.
2. **`optimized_code.py`**: the complete optimized code, preserving original interface signatures and behavior. Mark key changes with comments. Call it runnable only after compilation and import checks pass.
3. **`audit_report.html`**: a self-contained diagnostic report built from `assets/report_template.html`. Contains: health score + verdict, the sorted finding list (four-part entries with ④/③ tags), the optimized code with syntax highlighting, a before/after diff of key changes, a runtime-prerequisites section, and a tradeoffs section (what was deliberately left unchanged and why).

Build the report as structured JSON and render it with
`python scripts/render_report.py <report.json> <audit_report.html>`. Do not replace template
placeholders manually or insert raw HTML. The renderer validates required fields and HTML-escapes
all source-derived and user-derived text.

### Apply-to-original (confirmation gate)

Do NOT modify the user's original file by default. If the input was a pasted snippet, there is no original file to modify. If the input was a file on disk:

1. Present the modification plan (the diff from the report) to the user.
2. Ask explicitly: "Apply these changes to the original file?"
3. Only on user confirmation, write the optimized code into the original file path.
4. Rely on the user's git history (or the assistant's ability to revert on request) as the safety net — do not create `.bak` backup files.

## Boundaries

- Refactor, do not rewrite. Preserve observable behavior; change structure, not business logic.
- Do not change external interface signatures (function names, public API, parameter contracts).
- Extracted helpers must not replace or rename existing public entry points; keep compatibility wrappers when needed.
- Do not speculate about missing context. If a decision needs information the code does not provide, ask the user rather than guessing.
- Only audit Python. If the user provides non-Python code, decline and explain the scope.
- If `scripts/analyze.py` fails or the file has syntax errors, fall back to a manual read-based audit and note that static-analysis data is unavailable — do not crash the workflow.
- If third-party imports are unavailable, report the prerequisite and do not label the optimized artifact runnable.

## Resources

### scripts/analyze.py
Static analyzer. Input: a Python file or directory. Output: JSON with per-function cyclomatic complexity, duplicate code blocks, qualified hub functions (called from multiple non-recursive sites), and deduplicated I/O/DB call points. Used in Step 2 (recon) and Step 4 (hard evidence). Run as `python scripts/analyze.py <path>`. Project scans exclude common dependency, VCS, and cache directories. Robust to UTF-8 BOMs, syntax errors, empty files, and non-Python input — never crashes, always emits valid JSON.

### scripts/check_interface.py
Compatibility checker. Compare the original and optimized files with `python scripts/check_interface.py <original.py> <optimized.py>`. It reports missing or changed top-level functions/classes, class methods, and signatures. A non-zero result blocks the runnable claim until compatibility is restored.

### scripts/render_report.py
Safe report renderer. Read structured report JSON, validate required fields, escape all text, and fill `assets/report_template.html`. Run as `python scripts/render_report.py <report.json> <audit_report.html>`. Do not generate the final report with manual string replacement.

### references/ai_code_smells.md
The six AI-specific smells with definitions, detection features, before/after examples, and notes on why AI-generated code tends to exhibit each. Load in Step 3.

### references/perf_antipatterns.md
The seven Python performance anti-patterns with symptoms, complexity magnitude, fix, and expected gain. Load in Step 4.

### references/refactor_playbook.md
Refactor patterns used when prescribing fixes: extract function, replace if-elif chain with dict dispatch, early-return to reduce nesting, generator over list, strategy pattern, and more. Load when composing fixes in Step 5 and Step 6.

### assets/report_template.html
Self-contained HTML template for the diagnostic report. No external dependencies. Fill it only through `scripts/render_report.py`. Used in Step 6.
