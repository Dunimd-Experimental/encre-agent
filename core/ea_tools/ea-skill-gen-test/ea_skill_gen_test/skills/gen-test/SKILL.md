---
name: gen-test
description: Test generation - unit tests, edge cases, error paths, and integration tests following project conventions
aliases: [test, unittest, spec]
when_to_use: ".py .rs .js .ts .go .java"
argument_hint: "[files or modules to generate tests for]"
user_invocable: true
hidden: true
context: inline
---

## Test Generation Mode - QA Engineer

You are generating tests for: **{{args}}**

If no target was provided above, assume the specified code. Read the code under test fully before writing tests - never test a function you haven't read.

### When to Use
- Generate unit / edge-case / error-path tests for existing code
- Add coverage to a module that has weak or no tests
- Characterize legacy behavior before a refactor (lock current behavior with tests)

### When NOT to Use
- **Run the existing tests** -> `test_run` (gen-test *writes* tests; it doesn't execute them)
- **Review test quality** -> `code-review` the test file
- **Lint or format tests** -> `lint_format`
- **Fix the code a failing test exposed** -> `file_edit` the source, then `test_run` to confirm green

### Testing Strategy

**Analyze the code first:**
- What are the public interfaces? What inputs do they take?
- What are the happy paths, edge cases, and error conditions?
- What are the dependencies - can they be mocked?
- What existing tests exist? Study their patterns and style

**Test Types to Generate (in priority order):**
1. **Unit tests** - Test individual functions/classes in isolation. Mock external dependencies.
2. **Edge case tests** - Empty inputs, null/None, boundary values, large inputs, invalid types
3. **Error path tests** - Expected exceptions, error return values, failure handling
4. **Integration tests** - Test how components work together (if applicable)

**Test Quality Standards:**
- Each test tests ONE thing - one assertion per test where possible
- Test names describe the scenario and expected outcome: `test_[function]_[scenario]_[expected]`
- Use Arrange-Act-Assert pattern consistently
- Tests must be deterministic - no flaky dependencies on time, random, or external state
- Mock at the boundary, not internally - mock external services, not internal helpers
- Clean up any resources created during tests

**Detection & Coverage:**
- Identify any code paths that are hard to test - suggest refactoring to improve testability
- Check that error handling code is tested (not just happy paths)
- Ensure assertions actually verify the behavior, not just that no exception was thrown

**Framework Note:**
Match the test framework already used in the project. If none exists, use pytest for Python, Vitest/Jest for TypeScript, or the language-appropriate standard.

### Common Pitfalls
- **Testing the implementation, not the behavior** - asserting internal state or call counts couples the test to the implementation; a valid refactor then fails the test. Assert on observable outputs and contract.
- **Happy-path-only tests** - green suite that misses null inputs, empty collections, boundary values, and error paths gives false confidence. Always enumerate edge cases; bugs live where the code wasn't tested.
- **Over-mocking** - mocking every internal collaborator makes the test pass but proves nothing about real integration. Mock at system boundaries (external APIs, the clock, the filesystem), not internal helpers.
- **Non-deterministic tests** - depending on `datetime.now()`, `random`, dict iteration order, or test execution order creates flaky tests that fail on CI. Inject a clock/seed or sort first.
- **Weak assertions** - `assert result is not None` or "no exception was thrown" verifies nothing. Assert the actual expected value, including the error case's message/type.
- **Tests that don't run** - generating a test file but never executing it. Run `test_run` after writing; a test that doesn't even collect is a false claim of coverage.
- **Ignoring existing test conventions** - every project has a style (fixture usage, naming, where tests live). Read a sibling test file first and match it; a one-off style makes the suite inconsistent.
- **Testing mocked behavior instead of the real contract** - if you mock the dependency's return, you're testing that your mock returns what you told it to, not that the real integration works. Keep at least one integration test against the real thing.

### Pairing with Other Tools
- `file_read` - read the code under test before writing tests
- `grep` - find existing test files to match conventions, and all call sites of the function being tested
- `test_run` - execute the generated tests; iterate until green
- `lint_format` - keep generated tests consistent with project style
- `refactor` - if gen-test finds the code untestable, hand to refactor to improve testability, then re-generate
- `code-review` - review the generated tests for quality (behavior vs implementation coupling)

