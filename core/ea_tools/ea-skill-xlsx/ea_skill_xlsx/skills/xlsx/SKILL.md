---
name: xlsx
description: Read, write and inspect Excel workbooks (.xlsx/.xls) and CSV — cells, worksheets, and values only, with openpyxl fallback for formulas and formatting.
aliases:
  - excel
  - spreadsheet-doc
when_to_use: ".xlsx .xls .csv"
argument_hint: "[path to spreadsheet file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

# Spreadsheet (.xlsx / .xls)

`spreadsheet` reads and writes tabular files through openpyxl (Excel) and the standard library (CSV). Reads return the header plus up to 100 rows and a total row count; writes create or overwrite a file from CSV text or a JSON array of arrays; `list_sheets` enumerates worksheets. It stores **values only** — no formulas, no styling.

## When to Use

- Read cells from a workbook a user attached or pointed at.
- List worksheets before reading a multi-sheet workbook.
- Write a deliverable table (`.xlsx` or `.csv`) from computed results.
- Convert between CSV text and an Excel file.
- Check the scale of a dataset (total rows) before deciding whether to process it in code.

## When NOT to Use

- Plain structured text (CSV/JSON/YAML) → `file_read` / `data-files`; only use `spreadsheet` for binary Excel.
- Charts and dashboards → `chart` once the numbers are in hand.
- Heavy analytics, joins or aggregation → `notebook` with pandas; this tool reads at most 100 rows per call.
- Formulas, cell formatting, charts, named ranges → `terminal` with openpyxl/pandas.
- Word or slide decks → `document` / `presentation`.
- SQL result sets → `database`.

## Tools You Use

| Task | Tool | Action / key params |
|---|---|---|
| Worksheet names | `spreadsheet` | `action=list_sheets`, `file_path` |
| Read header + rows | `spreadsheet` | `action=read`, `file_path`, `sheet_name` |
| Write/overwrite | `spreadsheet` | `action=write`, `file_path`, `data` |
| Excel with formulas/formatting | `terminal` | openpyxl / pandas script |
| Full-dataset computation | `notebook` | read the whole file, compute, write out |
| Visualize | `chart` | `chart_type`, inline `data`, `output_path` |
| Search exported CSV | `grep` | literal or regex over the exported file |
| Build the Excel payload | `json_tool` | validate the array-of-arrays string |

### Parameter Notes

- `data` is a **string**: raw CSV text for `.csv`, JSON text decoding to an array of rows for Excel, e.g. `[["metric","value"],["revenue",184000]]`.
- `range` is advisory only and is **not enforced** — do not rely on it to limit output.
- `write` replaces the entire file or sheet; there is no append and no cell-level update.
- Reads stop at 100 rows; the returned total count tells you whether more exists.
- CSV values come back as strings — coerce numerics yourself.

## Workflows

### Read a workbook

1. `spreadsheet` → `action=list_sheets` (never assume a single `Sheet1`).
2. `spreadsheet` → `action=read` with the right `sheet_name`; look at the header and first rows.
3. Compare the returned total row count against 100: if it is larger, switch to `notebook` for anything that needs the full data.
4. Report which sheet you read.

### Write a workbook

1. Decide the headers, then build the JSON array of rows.
2. `spreadsheet` → `action=write` with `file_path`, `sheet_name`, `data`.
3. Verify by re-reading with `action=read` and checking the first rows and the total count.

### Excel to CSV / JSON

1. `spreadsheet` → `action=read` (or `notebook` for a full export).
2. `file_write` the result in the target format.
3. Verify with `file_read` or `grep` over the exported file.

### Heavy transformation

`terminal` with pandas or openpyxl: read the workbook, transform, write the output, then verify with `spreadsheet` `action=read`.

## Examples

```json
{
  "action": "write",
  "file_path": "D:/out/metrics.xlsx",
  "sheet_name": "Q3",
  "data": "[[\"metric\",\"value\"],[\"revenue\",184000],[\"churn\",0.032]]"
}
```

```json
{
  "action": "read",
  "file_path": "D:/data/sales.xlsx",
  "sheet_name": "Q3"
}
```

## Best Practices

- Always `list_sheets` first on an unknown workbook.
- Read a sample before reading a lot: a large sheet floods context.
- Write clean tabular values; do not embed formulas as strings.
- Keep headers exact when writing, so downstream readers line up with the source.
- Always verify a write by reading it back.
- Prefer `.csv` for interchange and `.xlsx` when the user needs a formatted workbook.

## Common Pitfalls

- openpyxl is not installed, so every `.xlsx` action fails.
- Reading a whole sheet blindly and blowing the context on a large workbook.
- Assuming a single sheet on a multi-sheet workbook.
- Trusting `range` to have limited the read — it has not.
- Writing formulas as strings and expecting them to evaluate.
- Overwriting a file or sheet silently: `write` replaces everything.
- Forgetting that CSV values are strings, so numeric comparisons fail.

## Anti-patterns

- Using this tool for a 500k-row aggregation.
- Building a workbook by calling `write` once per row.
- Assuming `read` returned the whole sheet when the total count is higher than the rows shown.
- Using `spreadsheet` to read a Word file renamed to `.xlsx`.

## Pairing with Other Tools

- `chart` — visualize the numbers once read.
- `notebook` — compute over the full dataset.
- `json_tool` — build and validate the JSON payload for Excel writes.
- `terminal` — pandas/openpyxl for formulas, formatting, named ranges.
- `grep` — search across an exported CSV for a value.
- `document` / `pdf` — the narrative around the table.
