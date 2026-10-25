---
name: tool-json-tool
description: "Validate, format, minify, flatten, schema-infer or dot-path query a JSON document from an inline string or a file."
hidden: true
context: inline
---

# tool-json-tool

`json_tool` is the structured replacement for `jq` one-liners and `python -m json.tool`. Give it inline `data` or a `file_path` and pick an `action`: check well-formedness, pretty-print, strip whitespace, render a flat key:value view, infer a JSON-Schema-style type tree, or resolve a dot-notation path. Output is structured, so nested values stay machine-readable.

## When to Use

- Check whether a config or payload parses: `action="validate"`.
- Pretty-print a minified response before reading it: `action="format"`.
- Shrink a payload before sending or storing: `action="minify"`.
- Inspect the key structure of an unfamiliar document: `action="schema"` or `action="transform"`.
- Pull one field out of a large object: `action="query"` with `query="data.items[0].name"`.

## When NOT to Use

- Editing arbitrary text files — use `file_edit` / `apply_patch`.
- YAML, TOML or XML — this tool is JSON-only.
- Deep analytics over large arrays (aggregation, sorting, joins) — do it in code with `terminal`, or query a slice first.
- Regenerating or reformatting a file you did not inspect; `format`/`minify` rewrite it in place.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `validate`, `format`, `minify`, `transform`, `schema`, `query`. |
| `data` | string | no | JSON text to process; required when `file_path` is absent. |
| `file_path` | string | no | Path to a JSON file; required when `data` is absent. Rewritten in place by `format`/`minify`. |
| `query` | string | no | Dot-notation path for `query`, e.g. `data.items[0].name`. |
| `indent` | integer | no | Indent width for `format`; default 2. |
| `sort_keys` | boolean | no | Sort object keys in output; default false. |

### Parameter Notes

- Exactly one of `data` / `file_path` supplies the input. With `data`, `format`/`minify` return the result without touching disk; with `file_path` they **overwrite** the file.
- `query` uses dot segments plus `[index]`; it does not support filters, wildcards or expressions.
- `indent` applies to `format` only; `minify` ignores it.
- `transform` produces a human-readable flat rendering, not JSON — do not parse its output.

## Example

```json
{
  "action": "query",
  "file_path": "D:/data/catalog.json",
  "query": "plugins[0].name"
}
```

## Best Practices

- `validate` before `query`: a syntax error otherwise surfaces as an unrelated lookup failure.
- Prefer `file_path` for anything over a few KB; embedding large JSON in `data` wastes context.
- Use `schema` to learn the shape of an unknown API response before writing extraction code.
- When you must keep the original, copy the file first — `format`/`minify` are destructive.

## Common Pitfalls

- `query` on a missing key returns an empty/absent result rather than an error; verify each path segment.
- Numbers may be re-serialized in normalized form, so formatting can change `1.0` to `1.0`/`1` depending on the value.
- Trailing commas and comments are invalid JSON; `validate` will reject them.
- Big files with `data` hit request-size limits — use `file_path`.
- Key order changes when `sort_keys=true`; do not diff the output against the original expecting stability.

## Anti-patterns

- Shelling out to `jq` or `python -c` for a one-field lookup.
- Using `format` across a whole directory (it takes a single file).
- Treating `transform` output as JSON and re-parsing it.
- Editing JSON by hand when `json_tool` can validate the result.

## Pairing with Other Tools

- `file_read` — view the file after a rewrite.
- `apply_patch` — make targeted JSON edits, then `validate`.
- `rest_client` / `web_fetch` — fetch a payload, then inspect it with `query`/`schema`.
- `terminal` — real aggregation over large datasets.
