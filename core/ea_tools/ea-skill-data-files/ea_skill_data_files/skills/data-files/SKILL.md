---
name: data-files
description: Read, write, convert, validate, and reshape structured data in CSV, JSON, JSONL, XML, YAML, and TSV. Use for format conversion, schema checks, filtering, projecting, joining, and aggregating tabular or nested data.
aliases:
  - structured-data
  - csv-json
when_to_use: ".csv .json .jsonl .xml .yaml .yml .tsv"
argument_hint: "[path to data file or task description]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## Structured Data File Processing

You are processing a structured data file: **{{args}}**

## When to Use

- Read CSV, JSON, JSONL, XML, YAML, or TSV content and report on it.
- Convert between structured formats (CSV to JSON, YAML to JSON, JSON to TSV).
- Validate structure: required fields, value types, schema conformance, duplicate keys.
- Reshape data: filter, project columns, aggregate, join two datasets.
- Move data into or out of a spreadsheet (`.xlsx`).

## When NOT to Use

- **Visualizing data** — once you have the values, use the `chart` tool or the `chart-visualization` skill.
- **Excel `.xlsx` / `.xls` files** — binary format; use the `spreadsheet` tool or the `xlsx` skill, not text parsers.
- **Deep statistical analysis, pivot tables, or SQL over many files** — use the `data-analysis` skill (DuckDB).
- **A single small config file** — `file_read` is enough; do not build a parser for 20 lines.
- **Log files** (`.log`, `.err`) — use the `debug` skill; they are not structured.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Peek / read a small file | `file_read` | `path`, `offset`, `limit` |
| Write converted output | `file_write` | `path`, `content` |
| Heavy parsing, conversion, reshape | `terminal` | `command` — Python one-liners or scripts (`json`, `csv`, `pandas`) |
| Row counts and coarse filtering | `terminal` | `command` — `wc -l`, `head`, `grep` |
| Search a value or key inside data | `grep` | `pattern`, `path` |
| Move data into `.xlsx` | `spreadsheet` | `action: write`, `file_path`, `data`, `sheet_name` |
| Move data out of `.xlsx` | `spreadsheet` | `action: read`, `file_path`, `sheet_name` |

## Workflow

### 1. Identify the format

Check the extension, then peek at the first lines with `file_read` to confirm the delimiter, whether a header row exists, and how deep the nesting goes. Never assume: a `.csv` can be pipe-delimited or headerless, and a `.json` file can actually be JSONL.

### 2. Size before content

For anything beyond a few hundred lines, get the shape first and only then read the payload:

```
wc -l data.csv
```

Reading a 50 MB file wholesale floods the context and truncates the answer. Sample, count, then slice.

### 3. Choose the operation

- **Read** — `file_read` for small files; `terminal` with Python for large or deeply nested ones.
- **Convert** — parse with a typed library, then `file_write` in the target format.
- **Validate** — check required fields, types, and schema; report every mismatch explicitly rather than fixing silently.
- **Reshape** — `terminal` with Python (`csv`, `json`, or `pandas`) to filter, project, aggregate, or join; write the result to a new file.

### 4. Preserve types

When converting CSV to JSON, numbers must stay numbers and booleans must stay booleans. String concatenation turns `123` into `"123"`; a typed parser does not.

### 5. Verify the output

Read a sample of the written file and confirm the structure and types survived the round trip.

## Examples

### CSV to JSON, types preserved

```
python -c "import json,csv;rows=list(csv.DictReader(open('people.csv',encoding='utf-8')));rows=[{'id':int(r['id']),'name':r['name'],'score':float(r['score'])} for r in rows];open('people.json','w',encoding='utf-8').write(json.dumps(rows,ensure_ascii=False,indent=2))"
```

### Count rows and columns without loading everything

```
wc -l orders.csv
head -n 1 orders.csv
```

### JSONL to a JSON array

```
python -c "import json;objs=[json.loads(l) for l in open('events.jsonl',encoding='utf-8') if l.strip()];open('events.json','w',encoding='utf-8').write(json.dumps(objs,ensure_ascii=False,indent=2));print(len(objs),'records')"
```

### Validate required fields

```
python -c "import json;d=json.load(open('config.json'));req=['name','version','entry'];missing=[k for k in req if k not in d];print('missing:',missing or 'none')"
```

### Move a table into Excel

```json
{
  "action": "write",
  "file_path": "./report.xlsx",
  "sheet_name": "Summary",
  "data": "[[\"Region\",\"Orders\",\"Revenue\"],[\"North\",1240,182300],[\"South\",980,141500]]"
}
```

## Best Practices

- Peek before parsing — read a few lines to confirm delimiter, header, and nesting.
- Count before reading — row counts and sizes first, payload second.
- Preserve types across conversions with typed parsers, never string ops.
- Validate explicitly: missing fields, type mismatches, and duplicate keys are the common bugs.
- Decide the record shape deliberately when converting CSV to JSON: list of objects, keyed by a natural key, or one file per object.
- Write UTF-8 and `ensure_ascii=False` when the data contains non-ASCII text.

## Common Pitfalls

- **Reading a huge data file wholesale** — floods context; sample first, count, then slice.
- **Losing types on conversion** — CSV to JSON via string ops turns numbers into strings.
- **Wrong delimiter** — TSV vs CSV vs pipe-delimited; peek first.
- **Ignoring nesting** — JSONL (one object per line) is not a JSON array; parse line by line.
- **Assuming a header row** — some CSVs are headerless; row 0 is data, not keys.
- **Trusting the extension** — `.json` that is actually JSONL, or `.csv` that is tab-separated.

## Pairing with Other Tools

- `chart-visualization` — render the values once they are extracted (`chart` tool).
- `data-analysis` — DuckDB SQL, pivots, and statistics over one or many files.
- `xlsx` / `spreadsheet` — move data into or out of Excel workbooks.
- `terminal` — the workhorse for parsing, conversion, and reshaping.
- `file_read` / `file_write` — peek at the source, write the output.
- `grep` — find a value or key inside a large data file.
