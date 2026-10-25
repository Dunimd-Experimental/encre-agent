---
name: tool-computer
description: "Single computer-control surface: observe the screen, drive browser or desktop, record/replay macros, delegate to a VLM."
hidden: true
context: inline
---

# tool-computer

The unified computer-control tool. One `action` field routes to either the browser backend or the desktop backend, screenshots come back as attached images (not base64 text), and an OCR digest lets text-only models read the screen. It also owns trajectory recording, macro libraries, state checkpoints, and VLM delegation.

## When to Use

- Any interaction with the machine's UI: look at the screen, click, type, hotkey, drag, scroll, clipboard.
- Browser automation where you want the same interface as desktop automation (`target: "browser"`).
- Multi-step pixel-level work you cannot describe precisely — hand a `goal` to `delegate` and let a VLM drive.
- Repeated UI sequences you want to record once and replay (`register_macro` / `execute_macro`).
- Long sessions where screenshots accumulate and you need to drop old ones (`compress_trajectory`).

## When NOT to Use

- Fine-grained in-page scripting where you already know the selectors — the `browser` tool is more direct.
- Reading a file, running a command, or editing text — filesystem/terminal tools are faster and cheaper.
- Anything that a headless HTTP call can do — use `rest_client` or `web_fetch`.
- Batch, deterministic operations; UI automation is the slowest and most brittle path available.

## Key Parameters

### Core routing

| Parameter | Type | Required | Description |
|---|---|---|---|
| `action` | string enum | yes | The operation. `observe`, `display_info`, `delegate`, interaction actions, meta-actions. |
| `target` | string | no | `browser` (default) or `computer` for cross-target actions (`click`, `type`, `screenshot`, ...). |
| `dry_run` | boolean | no | Preview the action without mutating the UI. |
| `expect_change` | boolean | no | Flag clicks that produce no visible change (dead element detection). |
| `coord_space` | string | no | Coordinate space for desktop actions; physical screenshot pixels by default. |
| `timeout` | number | no | Action timeout. |
| `timeout_ms` | number | no | Millisecond timeout for waits and delegated loops. |

### Pointing and input

| Parameter | Type | Required | Description |
|---|---|---|---|
| `x`, `y` | integer | no | Target point for coordinate actions. |
| `x2`, `y2` | integer | no | Drag end point. |
| `button` | string | no | Mouse button for click variants. |
| `text` | string | no | Text to type, or visible text to match for `click_text` / `find_text`. |
| `query` | string | no | Search query for element lookup (`find_element_by_name`, `get_elements`). |
| `tag` | string | no | Element tag filter for element queries. |
| `name` | string | no | Accessible/control name, macro name, or library entry name depending on the action. |
| `control_type` | string | no | UI Automation control type filter for desktop element lookup. |
| `fuzzy` | boolean | no | Token-order matching for text lookup. |
| `exact` | boolean | no | Exact matching instead of substring/role matching. |
| `occurrence` | integer | no | 1-based index when several elements match. |
| `key` | string | no | Single key for `press_key` / `key`. |
| `keys` | array | no | Key combo for `hotkey`, e.g. `["Control", "Shift", "Esc"]`. |
| `scroll_amount` | integer | no | Wheel/trackpad delta for `scroll`. |
| `ms` | integer | no | Sleep duration for `wait`. |

### Browser/desktop DOM actions

| Parameter | Type | Required | Description |
|---|---|---|---|
| `selector` | string | no | CSS selector for DOM actions. |
| `fields` | object | no | `fill_form` map of selector → value. |
| `tab_index` | integer | no | Tab to switch to / close. |
| `option_value` | string | no | `select_option` value. |
| `option_by` | string | no | How `option_value` is matched (value/label/index). |
| `attr_name` | string | no | Attribute/property name for `get_attribute` / `get_property`. |
| `js_code` | string | no | JavaScript to execute in the page. |
| `max_text_chars` | integer | no | Truncate extracted text (`get_all_text`, OCR digest). |

### Screen observation

| Parameter | Type | Required | Description |
|---|---|---|---|
| `include_ocr` | boolean | no | Include the OCR text digest on `observe`. |
| `include_a11y` | boolean | no | Include the accessibility tree (needed for native Windows apps). |
| `min_text_length` | integer | no | Drop OCR fragments shorter than this. |
| `max_depth` | integer | no | Limit accessibility-tree depth. |
| `max_nodes` | integer | no | Limit accessibility-tree node count. |

### Dialogs, pickers, clipboard, files

| Parameter | Type | Required | Description |
|---|---|---|---|
| `accept` | boolean | no | `set_dialog_handler`: accept (`true`) or dismiss. |
| `prompt_text` | string | no | Text for `prompt()` dialogs. |
| `paths` | array | no | File paths for `set_file_chooser_handler` / `file_drop`. |

### Trajectory, macros, metrics

| Parameter | Type | Required | Description |
|---|---|---|---|
| `actions` | array | no | Action list for `register_macro` / `execute_macro`. |
| `steps` | array | no | Recorded steps for `replay_trajectory`. |
| `start` | integer | no | Start index for replay. |
| `stop_on_failure` | boolean | no | Halt a macro/replay at the first failing step. |
| `n` | integer | no | Count for `recent_screenshots`. |
| `keep_screenshots` | integer | no | `compress_trajectory`: how many recent screenshots to keep. |
| `state` | object | no | Checkpoint payload for `save_state` / `load_state`. |
| `template` | string | no | Template image path for `locate_on_screen`. |
| `template_name` | string | no | Named template stored in the library. |
| `confidence` | number | no | Match threshold for template location (0–1). |
| `category` | string | no | Library category for `library_search`. |
| `tags` | array | no | Library/macro tags. |
| `overwrite` | boolean | no | `library_save`: replace an existing entry. |
| `enabled` | boolean | no | Library entry enabled flag. |
| `author` | string | no | Library entry author metadata. |
| `description` | string | no | Library entry description. |
| `source` | string | no | Origin of a recorded macro/trajectory. |
| `bundle` | string | no | Bundle path/id for `export_bundle` / `replay_bundle`. |
| `variables` | object | no | `{{key}}` substitutions for bundle replay. |
| `restore_state` | boolean | no | Bundle replay: restore the saved browser state first. |
| `include_state` | boolean | no | Bundle export: embed browser state. |
| `include_full_result` | boolean | no | Return full per-step results instead of a summary. |
| `goal` | string | no | Natural-language objective for `delegate`. |
| `max_steps` | integer | no | Step cap for `delegate`. |

### Parameter Notes

- `observe` is the entry point for screen work: it returns an attached image plus OCR text, cursor position, and DPI/geometry. Re-`observe` after acting to verify.
- `delegate` requires a VLM backend (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) and is far slower than direct actions. Use it only when you cannot name the target.
- The session enforces a step budget (default 200). Once exhausted, dispatch is refused with an explicit error until you call `reset`.
- `compress_trajectory` drops screenshots older than `keep_screenshots`; the textual history stays.
- Macro/library actions persist to disk, so a replayed macro can drift if the UI changes.

## Example

Look, then act, then verify:

```json
{ "action": "observe", "include_ocr": true, "include_a11y": false }
```

```json
{ "action": "click", "target": "desktop", "x": 640, "y": 412, "expect_change": true }
```

Type into a focused field and confirm:

```json
{ "action": "type", "target": "desktop", "text": "encre-plugin build" }
```

```json
{ "action": "hotkey", "keys": ["Control", "Enter"] }
```

Record a reusable sequence:

```json
{
  "action": "register_macro",
  "name": "open-settings",
  "description": "Open Encre Agent Desktop settings via the menu",
  "actions": [
    { "action": "click_by_role", "target": "browser", "role": "button", "name": "Settings" }
  ]
}
```

Delegate a fuzzy goal:

```json
{ "action": "delegate", "goal": "Open the settings panel and enable dark mode", "max_steps": 12, "target": "desktop" }
```

## Best Practices

- Start every screen task with `observe`; never guess coordinates from a previous session or a different DPI.
- Prefer named/role/text targeting over pixels; fall back to coordinates only when the element has no usable name.
- Set `expect_change: true` on clicks in a long flow so dead clicks surface immediately instead of silently derailing the task.
- Use `dry_run: true` when you are unsure what an action will hit.
- Call `compress_trajectory` periodically in long sessions; screenshots dominate context cost.
- `reset` after finishing a task so the next one starts with a fresh budget.
- On native apps, enable `include_a11y: true` — OCR alone misses unlabeled controls.

## Common Pitfalls

- Confusing coordinate spaces: browser coordinates are viewport CSS pixels, desktop coordinates are physical screenshot pixels unless `coord_space` says otherwise.
- Acting without re-observing after a click that changes layout; the second click lands on the wrong element.
- Forgetting that `target` defaults to `browser` — a desktop click without `target: "desktop"` silently goes to the page.
- Running out of the 200-step budget mid-flow and interpreting the refusal as a broken tool.
- Delegating a task a single `observe` + `click` would solve; the VLM loop is minutes, not seconds.
- Replaying a macro recorded on a different screen resolution or app version.

## Anti-patterns

- Screenshotting the full page after every keystroke.
- Building a 30-step macro for a one-off task.
- Using `delegate` as a general planner for tasks that need file edits.
- Retrying `locate_on_screen` with the same stale template after a UI redesign.
- Leaving `expect_change` off and discovering at step 20 that step 3 never worked.

## Pairing with Other Tools

- `browser` — when you want a narrower, page-only surface with the same semantics.
- `question` — to ask the user to take over when a login/CAPTCHA appears.
- `todo` — to track long UI workflows step by step.
- `agent` — to parallelise independent UI exploration without polluting the main trajectory.
- `image` — to crop/annotate screenshots before re-reading them.
