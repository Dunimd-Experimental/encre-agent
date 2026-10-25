---
name: pytest-forge
description: >-
  Unit test auto-generation + coverage blind-spot analysis Skill (iFLYTEK AI Data Intelligence Analysis and Application Skill Development Challenge
  · Track 2 "Testing and Quality Assurance" competition entry). Contains two complementary sub-capabilities forming a complete intelligent testing loop:
  (1) Unit test auto-generation - input Python source code, automatically extract functions/classes/methods based on ast static parsing,
  generate directly runnable pytest tests (main path smoke + boundary smoke) and output red/green report after actual execution;
  (2) Coverage blind-spot analysis - input source code + existing tests, measure actual line coverage via coverage.py (fallback to static matching if unavailable),
  identify uncovered functions/methods, rank by risk, and auto-generate gap test skeletons. Designed with graceful degradation for complex/incomplete code, syntax errors,
  import failures, async functions, etc., ensuring "generated test files are always syntactically valid, test suites always runnable,
  and never crash" - perfectly hitting the competition's high-robustness requirement. Trigger keywords: generate unit tests, test generation, auto-write tests, pytest generation,
  test case auto-generation, coverage blind spots, test coverage analysis, find gaps, fill missing tests, unit test generator,
  generate tests, testing and quality assurance.
---

# Unit Test Auto-Generator (Pytest Forge)

> iFLYTEK AI Data Intelligence Analysis and Application Skill Development Challenge · Track 2 (Testing and Quality Assurance) competition entry
> Value proposition: **Give us source code, and we return a pytest test suite that runs, exposes defects, and never crashes.**

---

## Use Cases
- Received a Python module and don't know where to start writing tests, or find writing tests tedious and time-consuming
- Want to quickly get a test skeleton, then manually fill in business assertions (TDD / regression baseline)
- Need to reliably produce tests for complex/legacy/possibly syntax-error code without the tool itself crashing
- Live demonstration: generate -> run -> red/green report, intuitive proof of "actionable + high robustness"

## Trigger Keywords
Generate unit tests / test generation / auto-write tests / pytest generation / test case auto-generation /
help me write tests / write tests for this function / coverage blind spots / test coverage analysis / find gaps /
fill missing tests / unit test generator / generate tests

## Input
- `-i` / `--input`: Python source file path (required)
- `-o` / `--out`: Output directory (default `./ut_output`), contains `test_<module>.py` and `report.md`
- `--no-run`: Only generate test file, do not run pytest (optional)

## Workflow (strictly sequential)
1. **Robust reading**: Sniff encoding in the order utf-8 -> gbk -> gb18030 -> latin-1, Chinese source code won't crash.
2. **AST static parsing**: Extract module-level functions, classes and their `__init__`/public methods (including `async`).
   Parse failure (SyntaxError) -> enter **degraded mode**, generate "all skip" placeholder tests, **exit normally, no crash**.
3. **Safe value inference**: Infer dummy values based on parameter type annotations / parameter names (`int->0`, `str->""`, `list->[]`,
   `dict->{}`, `bool->False`...), use default values if available; never reference undefined variables, ensuring generated code is syntactically valid.
4. **Generate test file**:
   - Each function: `test_<func>_happy` (main path smoke) + `test_<func>_boundary` (boundary smoke)
   - Each class: `test_<Class>_init` (instantiation) + each method `test_<Class>_<method>_happy`
   - `async` functions/methods wrap calls using standard library `asyncio.run(...)`, **no dependency on pytest-asyncio plugin**
   - Target module loaded via `importlib` by absolute path; all test cases are `skip`ped if import fails, suite still runs normally
5. **Actually run pytest**: Capture pass/fail/skip and timeout (180s), write results to `report.md`;
   if pytest is not installed, only generate and prompt, zero-dependency risk.
6. **Output report**: `report.md` contains overview, generation notes, robustness notes, pytest results.

## Generation Rules Quick Reference
- Main path test: Call with inferred "reasonable dummy values", verify no exception thrown (smoke)
- Boundary test: Call with boundary values (0 / empty string / empty container / None), `skip` if exception triggered and prompt for manual confirmation
- Class methods: First try instantiation with inferred parameters, fall back to no-arg constructor on failure; `skip` entire group if instantiation fails
- All assertion points marked with `# TODO: fill in business assertions` - generated tests are **skeletons**, assertions need manual completion

## Robustness Design (Competition Core Scoring Points)
- ✅ With complex/incomplete code, **always produces syntactically valid** test files (no SyntaxError)
- ✅ Target module syntax error / import failure -> generate "all skip" placeholder tests, **exit without error**
- ✅ `async` wrapped with `asyncio.run`, **no third-party plugins required** to run
- ✅ Target import failure -> test cases `skip`ped rather than collection error, **suite always runs**
- ✅ Single file function limit 60, truncate beyond and explicitly prompt in report, prevent huge files from hanging
- ✅ All three layers of exceptions (encoding/parsing/running) are caught, tool itself never crashes

## Running (Local)
Dependencies: Python 3.13 (managed venv `envs/default`) + pytest.
```shell
PY="$HOME/.workbuddy/binaries/python/envs/default/Scripts/python"
SKILL_DIR="$HOME/.workbuddy/skills/pytest-forge"

# Generate and run (recommended, comes with red/green report)
$PY "$SKILL_DIR/scripts/generate_tests.py" -i <source.py> -o ./ut_output

# Generate only, no run
$PY "$SKILL_DIR/scripts/generate_tests.py" -i <source.py> -o ./ut_output --no-run
```
Output:
- `ut_output/test_<module>.py`: Generated pytest test file
- `ut_output/report.md`: Generation overview + pytest results (pass/fail/skip)

## Sub-capability 2: Coverage Blind-Spot Analysis (analyze_coverage.py)

> Forms a "combination punch" with sub-capability 1: first cover the code with tests (generate_tests), then find gaps (analyze_coverage).

### Use Cases
- Already have some tests, want to know **which functions/methods are not tested at all**, and in what priority to fill them
- Want to use real line coverage data (not guesswork) to guide test supplementation, reducing defect debugging time
- Live demonstration: provide source code + existing tests -> output blind-spot list + risk-ranked + auto-generated skeleton

### Input
- `-i` / `--input`: Python source file (required)
- `-t` / `--tests`: Existing test file or directory (optional; omit to treat everything as a blind spot and suggest using sub-capability 1 for generation from scratch)
- `-o` / `--out`: Output directory (default `./coverage_report`), contains `report.md` and `test_<module>_gaps.py`
- `--mode`: `auto` (default, prioritize coverage measurement, fall back to `static` on failure) / `coverage` / `static`

### Workflow
1. **Robust reading + AST parsing**: Extract all testable units (module functions, class methods including `__init__`), record line range/branch complexity/publicity; give degradation note without crashing on parse failure.
2. **Coverage determination**:
   - `coverage` mode: Use `coverage.py` (Python API, `include` precisely locks single file, run pytest in same process) for actual line coverage measurement; **exclude `def` signature lines** to avoid mistaking imports as calls; single-line function coverage cannot distinguish import/call, degrades to name reference determination.
   - On failure, automatically degrade to **static name matching** (function/method name appearing in test source is considered covered).
3. **Blind-spot identification + scoring**: Blind spot = uncovered unit; score = (1 - coverage) x (1 + 0.15 x branch count) x publicity weight (public 1.0 / internal 0.4), sorted by score → priority high/medium/low.
4. **Auto-generate skeleton**: For gap units, reuse `generate_tests.build_test_file` to generate directly runnable `test_<module>_gaps.py` (happy + boundary smoke, syntactically valid, skip on import failure).
5. **Output report**: `report.md` contains overview, per-item coverage details, blind-spot list sorted by risk, and gap-filling suggestions.

### Running
```shell
PY="$HOME/.workbuddy/binaries/python/envs/default/Scripts/python"
SKILL_DIR="$HOME/.workbuddy/skills/pytest-forge"

# Blind-spot analysis (coverage measurement, auto-degradation)
$PY "$SKILL_DIR/scripts/analyze_coverage.py" -i <source.py> -t <existing_test.py> -o ./coverage_report

# Blind-spot only, no coverage dependency (purely static)
$PY "$SKILL_DIR/scripts/analyze_coverage.py" -i <source.py> -t <existing_test.py> --mode static
```

## Combination Workflow (End-to-End)
1. **Cover**: `generate_tests.py -i src.py` -> generate smoke tests for all functions/methods, `report.md` gives red/green.
2. **Find gaps**: Pass the generated (or your own) tests to `analyze_coverage.py -i src.py -t <tests>` -> find blind spots, rank by risk, auto-generate `test_<module>_gaps.py`.
3. **Fill**: Fill in business assertions at the `TODO` markers in `test_<module>_gaps.py`, closing the loop.
4. **Close**: Re-run `analyze_coverage.py`, blind spots should be zero (or only intentionally left ones).

> This closed loop directly addresses the two major pain points of the competition theme - "test verification" and "defect debugging time reduction" - with both sub-capabilities sharing the same robust skeleton generation base, providing differentiation and completeness.

## Competition Highlights Mapping (iFLYTEK Track 2: Testing and Quality Assurance)
- **Test generation**: Automatically produce structured unit test cases from natural language/source code, covering main paths and boundaries
- **Quality assurance**: Actually run tests to expose real defects (e.g., division by zero), presented as red/green report, closed-loop traceability
- **High robustness**: Stable operation with syntax errors / import failures / async / large files / Chinese encoding - no crashes, no data loss
- **Direct deployment**: Generated output is directly runnable pytest files, developers can immediately fill assertions and integrate into CI, aligning with competition theme
- **Demonstrability**: Generate -> run -> report in one click, judges can see on-site "input source code, output runnable tests and defect report"
- **Coverage blind-spot analysis (differentiator)**: Use real line coverage data to locate untested functions/methods, rank by risk, auto-generate skeletons, forming a "cover + find gaps" closed loop with unit test generation, significantly reducing defect debugging time, enhancing work differentiation and completeness
