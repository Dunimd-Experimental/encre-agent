---
name: tool-browser
description: "Drive the embedded Chromium in Encre Agent Desktop: navigate, click, fill forms, read JS-rendered pages, and screenshot."
hidden: true
context: inline
---

# tool-browser

Controls a real Chromium instance living inside the Encre Agent Desktop window. One tool, one `action` field, ~45 actions covering navigation, DOM interaction, extraction, tabs, keyboard, dialogs, accessibility, and raw CDP. Use it when the page must actually execute scripts or you must click through a flow; for static text use `web_fetch`, for search snippets use `web_search`.

## When to Use

- The content only exists after JavaScript runs (SPAs, infinite scroll, client-rendered dashboards).
- You must interact: log in, click through a wizard, fill a multi-field form, pick from a `<select>`.
- You need visual grounding: `screenshot` / `screenshot_viewport` and then act on coordinates.
- You need the accessibility tree (`a11y_snapshot`) or raw DevTools data (`execute_cdp`) that no static fetch can provide.
- A flow requires the user's own hands (login, CAPTCHA, 2FA, payment) and control must be handed over explicitly.

## When NOT to Use

- Plain keyword search — `web_search` returns content inline without a browser.
- Fetching one static URL/API response — `web_fetch`, which is faster and leaves no session state.
- Raw JSON API calls with headers/auth — `rest_client`.
- Bulk scraping where each page is static HTML — `web_fetch` in a loop is cheaper.
- Interacting with native desktop apps — use `computer` with `target: "desktop"`.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | The operation. See the action groups below. |
| `url` | string | no | Navigation target, or a plain query (routed to the configured search engine). |
| `selector` | string | no | CSS selector for DOM actions. Get valid ones from `get_page_structure`. |
| `request` | string enum | no | `agent_control` action: `user_takeover` or `agent_control`. |
| `x`, `y` | integer | no | Viewport pixel coordinates for `click_at`, `type_at`, `hover_at`, drag start. |
| `x2`, `y2` | integer | no | Drag end coordinates. |
| `text` | string | no | Text to match (`click_text`, `find_text`) or to type (`type`, `type_at`). |
| `full_page` | boolean | no | `screenshot`: capture the whole scrollable page instead of the viewport (default `false`). |
| `code` | string | no | `execute_js`: JS expression or block; must return a JSON-serializable value. |
| `timeout` | integer | no | `wait_for_selector` timeout in ms. |
| `fields` | object | no | `fill_form`: `{selector: value, ...}`. |
| `key` | string | no | `press_key`: a single key name. |
| `keys` | array | no | `hotkey`: combination such as `["Control", "c"]`. |
| `cookies` | array | no | `load_cookies`: list of cookie dicts. |
| `interesting_only` | boolean | no | `a11y_snapshot`: prune uninteresting nodes (default `true`). |
| `role` | string | no | `click_by_role`: ARIA role (`button`, `link`, `textbox`, ...). |
| `name` | string | no | `click_by_role` / `get_by_text_count`: accessible name. |
| `exact` | boolean | no | Exact name matching for role/text lookups. |
| `root_selector` | string | no | `a11y_snapshot`: restrict the tree to this CSS root. |
| `value` | string | no | `select_option`: option value, label, or index. |
| `by` | string enum | no | `select_option`: `value` (default), `label`, or `index`. |
| `index` | integer | no | `switch_tab` / `close_tab`: 0-based tab index. |
| `ms` | integer | no | `wait`: milliseconds to sleep. |
| `method` | string | no | `execute_cdp`: CDP method, e.g. `Network.enable`, `DOM.getDocument`. |
| `params` | object | no | `execute_cdp`: CDP parameters. |
| `accept` | boolean | no | `set_dialog_handler`: accept (`true`, default) or dismiss. |
| `prompt_text` | string | no | `set_dialog_handler`: text fed to `prompt()`. |
| `paths` | array | no | `set_file_chooser_handler`: file paths to inject. |
| `fuzzy` | boolean | no | `click_text`/`find_text`: match query tokens in order. |
| `occurrence` | integer | no | `click_text`/`find_text`: 1-based match index. |
| `max_chars` | integer | no | `get_all_text`: truncate output (default 200000). |

### Action Groups

- **Navigate** — `navigate`. Pass a URL or a plain query; a query goes to the configured search engine.
- **Map the page** — `get_page_structure` (interactive elements + bounding boxes + `center_x/center_y`), `a11y_snapshot`, `get_by_text_count`, `find_text`.
- **Click** — `click` (selector), `click_text`, `click_by_role`, `click_at` / `double_click_at` / `right_click_at` (coordinates), `hover` / `hover_at`, `drag`.
- **Type** — `type(selector, text)`, `type_at(x, y, text)`, `fill_form`, `select_option`, `press_key`, `hotkey`.
- **Read** — `get_all_text`, `get_text`, `get_html`, `get_attribute`, `get_property`, `execute_js`, `screenshot`, `screenshot_viewport`, `get_viewport`, `get_state`.
- **Wait / scroll** — `wait_for_selector`, `wait`, `scroll_to`.
- **Tabs** — `list_tabs`, `new_tab`, `switch_tab`, `close_tab`.
- **Session** — `save_cookies`, `load_cookies`, `close_session`.
- **Native dialogs & pickers** — `set_dialog_handler`, `set_file_chooser_handler` (install *before* triggering).
- **Advanced** — `execute_cdp` for raw Chrome DevTools Protocol.
- **Control handoff** — `agent_control` with `request: "user_takeover"` or `"agent_control"`.

## Example

```json
{
  "action": "navigate",
  "url": "https://example.com/login"
}
```

Then map the page and act on text rather than coordinates:

```json
{ "action": "get_page_structure" }
```

```json
{ "action": "type", "selector": "input[name=email]", "text": "agent@example.com" }
```

```json
{ "action": "click_by_role", "role": "button", "name": "Sign in" }
```

Coordinate fallback (from a screenshot, viewport pixels):

```json
{ "action": "click_at", "x": 412, "y": 268 }
```

Read the rendered result:

```json
{ "action": "get_all_text", "max_chars": 20000 }
```

## Best Practices

- Always follow `navigate` with `get_page_structure` (or `screenshot`) before acting. Coordinates and selectors go stale the moment the page re-renders.
- Prefer text/role matching (`click_text`, `click_by_role`) over CSS selectors, and CSS selectors over raw coordinates — in that order of robustness.
- Gate on readiness with `wait_for_selector` instead of guessing with `wait`; `ms` is a blunt instrument.
- Install `set_dialog_handler` / `set_file_chooser_handler` *before* the action that opens the dialog, otherwise the dialog blocks the page.
- For login, CAPTCHA, 2FA, or payment, hand over control: `agent_control` + `request: "user_takeover"`. Never ask the user for a password in chat.
- Reuse one session across steps; `close_session` resets cookies and storage, so only call it when the task is done or you need a clean identity.
- Keep extraction bounded: `max_chars` on `get_all_text`, `interesting_only: true` on `a11y_snapshot`.

## Common Pitfalls

- Clicking an element that is off-screen: it "fails" or hits the wrong target. `scroll_to(x, y)` first, then re-read the structure.
- Treating `agent_control` as a two-way switch that always succeeds. Returning control to the agent raises an approval prompt; silence for 60s counts as denied. A denial means find another way — do not immediately retry.
- Assuming the browser is always open. If actions fail with a connection error, ask the user to open the browser tab in Encre Agent Desktop.
- Matching visible text that appears several times and expecting the first one to be the right one; use `occurrence` or `get_by_text_count` to disambiguate.
- Using `execute_js` for navigation or clicks when a first-class action exists; JS bypasses the tool's own waits and error mapping.
- Expecting `get_html` of a SPA shell to contain data — read `get_all_text` or call the underlying API via `execute_js`/`rest_client`.
- Forgetting that `close_session` also drops saved cookies, which can log you out of a flow you intended to continue.

## Anti-patterns

- Screenshotting after every micro-step. Screenshots are expensive context; take one when you need to see layout, then act.
- Hard-coding pixel coordinates across a page you have not screenshotted in this session.
- Driving the browser for a task `web_fetch` can serve, just because the page renders in a browser for a human.
- Asking the user to paste a password instead of using `user_takeover`.
- Retrying the same failing selector five times without re-reading `get_page_structure`.

## Pairing with Other Tools

- `web_search` / `web_fetch` — cheaper first line for static content and search.
- `computer` — for native OS windows, clipboard, or when the task crosses browser and desktop.
- `rest_client` — when the page is really a JSON API you can call directly.
- `document` / `spreadsheet` — to persist extracted tables instead of leaving them in the transcript.
- `notify` — to tell the user a manual step (login) is waiting on them.
