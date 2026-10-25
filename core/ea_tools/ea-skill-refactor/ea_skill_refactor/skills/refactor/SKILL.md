---
name: refactor
description: Behavior-preserving code transformation - extract, rename, decouple, and restructure with zero regression
aliases: [restructure, cleanup, improve]
when_to_use: ".py .rs .js .ts .go .java"
argument_hint: "[files, modules, or components to refactor]"
user_invocable: true
hidden: true
context: inline
---

## Refactoring Mode - Code Transformation Expert

You are refactoring: **{{args}}**

If no target was provided above, assume the specified code. Read the code fully before refactoring - never refactor code you haven't read.

### When to Use
- Restructure code without changing behavior (extract, rename, decouple, simplify)
- Reduce duplication, complexity, or coupling in existing working code
- Prepare code for an upcoming feature by improving its structure first

### When NOT to Use
- **Fix a bug or change behavior** -> `file_edit` directly (refactoring preserves behavior; bug fixes do not)
- **Add a feature** -> implement it; refactor only as a separate, prior step if the structure blocks you
- **Review for issues** -> `code-review` (finding problems), then refactor to fix them
- **Rewrite from scratch** -> if the code is fundamentally wrong, a refactor is the wrong tool; rewrite instead

### Refactoring Principles

**Golden Rule: Preserve Behavior**
- The refactored code must produce exactly the same outputs for the same inputs
- Never change public API signatures unless explicitly requested
- Never change behavior while restructuring - refactoring and feature addition are separate concerns

**Refactoring Approach**
1. **Understand first** - Read the full code, understand its behavior, edge cases, and dependencies
2. **Identify improvement opportunities** - Duplication, complexity, coupling, naming, structure
3. **Plan the changes** - What will you change and in what order to minimize risk
4. **Execute incrementally** - One concern at a time, verify after each step
5. **Verify** - Run existing tests, check compilation, ensure no regression

**Common Refactoring Patterns**
- Extract Method/Function - break long functions into focused units
- Rename - make names reveal intent
- Introduce Parameter Object - group related parameters
- Replace Conditional with Polymorphism - simplify complex conditionals
- Extract Class - split god classes by responsibility
- Introduce Strategy/Factory - decouple object creation from usage

### Output
- Show all changes in unified diff format
- For each change, explain why it improves the code
- After all changes, summarize: what was improved, and what remains untouched

### Common Pitfalls
- **Refactoring without test coverage** - if there are no (or weak) tests, you cannot verify behavior is preserved. Either `gen-test` first, or stop and say so. Refactoring blind is gambling.
- **Mixing refactoring with behavior changes** - "while I'm here let me also fix this bug" makes the diff unreviewable and breaks the safety property. One or the other, never both in one pass.
- **Big-bang refactors** - restructuring half the codebase in one diff means no step can be verified. Refactor in small, individually-verifiable steps; run tests after each.
- **Changing public API "because it's cleaner"** - external callers depend on the current signature. Preserve it unless the user explicitly asked to change it.
- **Renaming without finding all call sites** - use `grep` to find every reference first; a rename that misses a caller breaks the build.
- **Trusting "tests pass" after a refactor** - if the tests don't actually exercise the refactored path, green means nothing. Check the coverage hit the changed lines.
- **Refactoring code that isn't finished** - if the feature is still in flux, refactoring now is wasted work the next change will undo. Wait until the code stabilizes.

### Pairing with Other Tools
- `gen-test` - generate characterization tests *before* refactoring if coverage is thin
- `test_run` - run the suite after each refactor step, not just at the end
- `grep` - find all call sites before renaming or changing a signature
- `lint_format` - verify style stayed consistent after restructuring
- `code-review` - review the refactor diff for behavior preservation before merging
- `lsp` - use go-to-definition / find-references for precise rename across the codebase

