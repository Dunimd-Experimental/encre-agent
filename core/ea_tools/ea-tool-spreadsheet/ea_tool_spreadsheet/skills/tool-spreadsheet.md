---
name: tool-spreadsheet
description: "Read, write or list worksheets in CSV and Excel files, capping reads at the first 100 rows plus a total count."
hidden: true
context: inline
---

# tool-spreadsheet

`spreadsheet` reads and writes tabular files: CSV through the standard library, `.xlsx`/`.xls` through openpyxl. Reads return a header plus up to 100 data rows and the total row count; writes create or overwrite a file from raw CSV text or a JSON array of arrays; `list_sheets` enumerates Excel worksheets.

## When to Use

- Inspect the columns and a sample of a CSV or workbook before processing it.
- Produce a deliverable table from computed results.
- Enumerate worksheets in a workbook and pick the right one with `sheet_name`.
- Convert between CSV text and an Excel file.
- Check scale (total rows) before deciding to process with code.

## When NOT to Use

- Heavy analytics, joins or aggregation — use `notebook` or `terminal` with pandas.
- Word documents or slide decks — use `document` / `presentation`.
- PDFs with tables — extract with `pdf`, then clean up here.
- Databases or SQL result sets — use `database`.
- Writing formulas or styling cells — unsupported; values only.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `read` (header + up to 100 rows), `write` (create/overwrite), `list_sheets`. |
| `file_path` | string | yes | Spreadsheet path; extension selects handler (`.csv` vs `.xlsx`/`.xls`). |
| `sheet_name` | string | no | Worksheet to target; defaults to the active sheet. Ignored for CSV. |
| `data` | string | no | Write payload: raw CSV text for `.csv`, JSON array of arrays for Excel (`[["a",1]]`). |
| `range` | string | no | Cell range hint for Excel reads (e.g. `A1:C10`); **parsed but not enforced**. |

### Parameter Notes

- `data` is a **string**: for Excel you pass JSON text that decodes to an array of rows, not a native array.
- `range` is advisory only — do not rely on it to limit output; use `sheet_name` or pre-truncate.
- `write` replaces the entire file/sheet; there is no append or cell-level update.
- Reads stop at 100 rows; the trailing count tells you whether there is more.

## Example

```json
{
  "action": "write",
  "file_path": "D:/out/metrics.xlsx",
  "sheet_name": "Q3",
  "data": "[[\"metric\",\"value\"],[\"revenue\",184000],[\"churn\",0.032]]"
}
```

## Best Practices

- `read` first to learn the columns; then write matching headers exactly.
- Use `list_sheets` on unknown workbooks before guessing `sheet_name`.
- Keep Excel writes to values; format the file in a spreadsheet app afterwards if needed.
- For more than 100 rows, read a sample here and do the heavy lifting in `notebook`.
- Prefer CSV for interchange and `.xlsx` when the user needs a formatted workbook.

## Common Pitfalls

- openpyxl is required for Excel; without it `.xlsx` actions fail.
- `range` not being enforced surprises people — a "limited" read can still return 100 rows of the wrong region.
- Overwriting `file_path` destroys existing content silently.
- CSV values are read as strings; numeric coercion is up to you.
- Mixed types in a column (numbers and text) round-trip inconsistently through Excel.

## Anti-patterns

- Using this tool for a 500k-row aggregation.
- Writing formulas as strings and expecting them to evaluate.
- Assuming `range` filtered the data.
- Building a workbook by repeatedly calling `write` per row.

## Pairing with Other Tools

- `notebook` — compute over the full dataset.
- `chart` — visualize the numbers this tool stores.
- `json_tool` — build/validate the JSON array for Excel writes.
- `document` / `pdf` — narrative and source documents around the table.
