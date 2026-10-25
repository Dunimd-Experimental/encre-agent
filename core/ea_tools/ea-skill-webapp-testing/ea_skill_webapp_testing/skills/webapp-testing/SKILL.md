---
name: webapp-testing
description: "Test local web applications with Python Playwright. Verify frontend behavior, debug UI bugs, capture screenshots, and read browser console logs. Includes a server-lifecycle helper for frontend plus backend stacks."
aliases:
  - playwright-testing
  - e2e-local
  - "ui verification"
argument_hint: What to verify in the app
user_invocable: true
hidden: true
---

# Web Application Testing

Write native Python Playwright scripts to drive a local app, inspect what actually rendered, and verify behavior. Two things make this reliable: the server is managed for you by a helper script, and selectors are discovered from the rendered page instead of guessed from source.

## When to Use

- Verifying that a UI flow actually works end to end (login, submit, filter, navigate).
- Debugging a frontend bug by reproducing it against the real app.
- Capturing screenshots as evidence of a rendered state.
- Reading console logs and failed network requests to find a runtime error.
- Checking that a static HTML page renders the expected elements.

## When NOT to Use

- Unit-testing a pure component or function — use `test_run`.
- Checking types, formatting, or static analysis — use `lint_format`.
- Testing a remote production site — these scripts target localhost.
- Screenshots for design review without assertions — use the `browser` tool directly.

## Setup

```shell
python -m pip install playwright
python -m playwright install chromium
```

Always launch chromium in headless mode.

## Helper Scripts

```
scripts/with_server.py       Manages server lifecycle, then runs your script
```

Run any script with `--help` first to see its usage. Do not read the script's source: these helpers are large, and reading them wastes context for no benefit. They exist to be called as black boxes.

`with_server.py` verified usage:

```
python scripts/with_server.py --server SERVERS --port PORTS [--timeout TIMEOUT] -- command
  --server SERVERS   Server command (can be repeated)
  --port PORTS       Port for each server (must match --server count)
  --timeout TIMEOUT  Timeout in seconds per server (default: 30)
  command            Command to run after server(s) ready
```

Single server:

```shell
python scripts/with_server.py --server "npm run dev" --port 5173 -- python your_automation.py
```

Backend plus frontend:

```shell
python scripts/with_server.py \
  --server "cd backend && python server.py" --port 3000 \
  --server "cd frontend && npm run dev" --port 5173 \
  -- python your_automation.py
```

The helper waits for each port, runs your command, then tears the servers down. Your script contains Playwright logic only; do not start or stop servers inside it.

## Choosing Your Approach

```
Is it static HTML?
    YES -> Read the HTML directly to identify selectors
             ├─ Sufficient  -> Write a script using those selectors
             └─ Not enough  -> Treat it as dynamic (below)
    NO  (dynamic app) -> Is a server already running?
             NO  -> Start it with scripts/with_server.py, then write the script
             YES -> Reconnaissance, then action:
                    1. Navigate and wait for networkidle
                    2. Screenshot or dump the DOM
                    3. Identify selectors from the rendered state
                    4. Act using the discovered selectors
```

## A Minimal Script

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://localhost:5173")
    page.wait_for_load_state("networkidle")   # wait for JS to execute
    page.locator("button", has_text="Submit").click()
    page.wait_for_selector("[role='status']")
    page.screenshot(path="result.png", full_page=True)
    print(page.content()[:2000])
    browser.close()
```

## Reconnaissance, Then Action

Never write selectors from the source tree when the rendered DOM is available.

1. Inspect the rendered state:

```python
page.screenshot(path="inspect.png", full_page=True)
print(page.content()[:4000])
print(page.locator("button").count())
```

2. Identify selectors from that output.
3. Write the action script with the discovered selectors and run it.

A second pass after a UI change is normal: render first, then act.

## Choosing Selectors

Prefer, in order: `role` (`page.get_by_role("button", name="Save")`), `text=` (`page.get_by_text("Submit")`), `has_text` on a locator, explicit IDs, then CSS. Avoid long compound CSS selectors and generated class names; they break on any refactor.

Always add a wait next to an action: `wait_for_selector`, `wait_for_load_state("networkidle")`, or `wait_for_timeout` for animations. A click without a wait passes on your machine and fails on the next run.

## Capturing Console Logs

Use the `examples/console_logging.py` pattern: attach a listener before navigating, collect the entries, and print them after the action. This is how you find a runtime error that a green screenshot hides.

## Tools You Use

| Task                            | Tool      | Note                                              |
| ------------------------------- | --------- | ------------------------------------------------- |
| Run the helper or a script      | `terminal`| Use `scripts/with_server.py --help` first         |
| Write the automation script     | `file_write` | Keep it to Playwright logic only               |
| Read an existing script         | `file_read` | Only your own scripts, never the helper source   |
| Find a route or page name       | `grep`, `glob` | Before navigating to it                     |
| Lint the script                 | `lint_format` | `file_paths` accepts a list                    |
| Interactive check without a script | `browser` | When you only need to look, not to assert        |

## Best Practices

- Use the helper scripts as black boxes; invoke them, do not read them.
- Use `sync_playwright()` for straightforward synchronous scripts.
- Always close the browser when you are done.
- Use descriptive selectors (`role=`, `text=`, `get_by_label`), not generated class names.
- Assert on visible text or roles, and screenshot the state you are asserting about.
- Print a slice of `page.content()` when something fails; it tells you what actually rendered.

## Common Pitfalls

- Inspecting the DOM before `networkidle` on a dynamic app. You read the loading shell, pick the wrong selectors, and then conclude the app is broken.
- Writing selectors from the component source instead of the rendered page. The build renames classes and wraps elements.
- Starting a server inside the script while the helper is also managing it. Pick one.
- Forgetting the trailing `--` before the command in `with_server.py`; argparse reads your script as flags.
- Leaving the browser open, which leaves a process holding the port on the next run.
- Reading `scripts/with_server.py` to understand it. Run `--help` instead.

## Examples

```
examples/element_discovery.py       Discover buttons, links, and inputs on a page
examples/static_html_automation.py  Drive a local file directly with a file:// URL
examples/console_logging.py         Capture console logs during automation
```

## Sources

- Helper: `scripts/with_server.py` (usage verified with `--help`).
- Examples: `examples/` at the skill root.
- Upstream: Anthropic, listed at skills.sh as `anthropics/skills/webapp-testing`.
