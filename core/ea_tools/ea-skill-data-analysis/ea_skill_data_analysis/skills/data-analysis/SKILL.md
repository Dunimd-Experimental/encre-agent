---
name: data-analysis
description: "Analyze Excel (.xlsx/.xls) and CSV files with an in-process DuckDB engine: inspect schemas, run SQL, aggregate, join across files, profile quality, and export to CSV, JSON, or Markdown."
aliases:
  - excel-analysis
  - duckdb
when_to_use: ".xlsx .xls .csv .tsv"
argument_hint: "[file paths and the analysis question]"
user_invocable: true
hidden: true
auto_activate: true
context: inline
---

## Data Analysis with DuckDB

Turn uploaded spreadsheets and delimited files into answers. Analysis runs through a bundled script that loads the files into an in-process DuckDB database and executes SQL against them.

- Inspect sheet names, columns, types, and row counts.
- Run arbitrary SQL: aggregations, filters, joins, window functions, CTEs, pivots.
- Produce statistical summaries for numeric and string columns.
- Join tables across multiple files in one query.
- Export query results to `.csv`, `.json`, or `.md`.
- Cache loaded data in a DuckDB file under the system temp directory, keyed by a SHA-256 of the inputs, so repeated queries against the same files start instantly.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Run inspect / query / summary | `terminal` | `command` — `python scripts/analyze.py --files <paths> --action <action>` |
| Inspect a source file quickly | `file_read` | `path` |
| Chart the results | `chart` | `chart_type`, `data`, `output_path` |
| Write the resulting spreadsheet | `spreadsheet` | `action: write`, `file_path`, `data` |

The script lives in `scripts/analyze.py` next to this document. Do not read the Python file — call it with the parameters below. It installs `duckdb` and `openpyxl` automatically on first use.

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--files` | Yes | Space-separated paths to `.xlsx`, `.xls`, or `.csv` files |
| `--action` | Yes | `inspect`, `query`, or `summary` |
| `--sql` | For `query` | SQL to execute against the loaded tables |
| `--table` | For `summary` | Sheet or table name to summarize |
| `--output-file` | No | Export target; format inferred from extension (`.csv`, `.json`, `.md`) |

## Workflow

### 1. Inspect the schema first

```
python scripts/analyze.py --files sales.xlsx --action inspect
```

This returns sheet names (or the file name for CSV), column names, types, non-null counts, row counts, and the first five rows. Always do this before writing SQL.

### 2. Write the query

Use the table names reported by `inspect`. Quote names that start with a digit or contain special characters: `"2024_Sales"`.

```
python scripts/analyze.py --files sales.xlsx --action query \
  --sql "SELECT category, COUNT(*) AS cnt, AVG(amount) AS avg_amount FROM Orders GROUP BY category ORDER BY cnt DESC"
```

### 3. Summarize distributions

```
python scripts/analyze.py --files sales.xlsx --action summary --table Orders
```

Numeric columns return count, mean, std, min, 25%, 50%, 75%, max, and null count. String columns return count, unique, top value, frequency, and null count.

### 4. Export what matters

```
python scripts/analyze.py --files sales.xlsx --action query \
  --sql "SELECT * FROM Orders WHERE amount > 1000" \
  --output-file ./outputs/high-value-orders.csv
```

## Table Naming

- **Excel** — each sheet becomes a table named after the sheet (`Sheet1`, `Sales`, `Revenue`).
- **CSV** — the file name without its extension (`orders.csv` becomes `orders`).
- **Multiple files** — all tables from all files are available in one query context, so cross-file joins work directly.
- **Special characters** — spaces become underscores; quote names that start with a digit.

## SQL Patterns

### Exploration

```sql
SELECT COUNT(*) FROM Orders;
SELECT DISTINCT category FROM Orders;
SELECT category, COUNT(*) AS cnt FROM Orders GROUP BY category ORDER BY cnt DESC;
SELECT MIN(order_date), MAX(order_date) FROM Orders;
```

### Aggregation and trends

```sql
SELECT category, DATE_TRUNC('month', order_date) AS month, SUM(revenue) AS total_revenue
FROM Sales GROUP BY category, month ORDER BY month;

SELECT customer_name, SUM(amount) AS total_spend
FROM Orders GROUP BY customer_name ORDER BY total_spend DESC LIMIT 10;
```

### Cross-file joins

```sql
SELECT s.order_id, s.amount, c.customer_name, c.region
FROM orders s JOIN Customers c ON s.customer_id = c.id WHERE s.amount > 500;
```

### Window functions

```sql
SELECT order_date, amount,
       SUM(amount) OVER (ORDER BY order_date) AS running_total,
       RANK() OVER (ORDER BY amount DESC) AS amount_rank
FROM Orders;
```

### Pivot-style month columns

```sql
SELECT category,
       SUM(CASE WHEN MONTH(order_date) = 1 THEN revenue END) AS Jan,
       SUM(CASE WHEN MONTH(order_date) = 2 THEN revenue END) AS Feb
FROM Sales GROUP BY category;
```

## Data Quality Checks

Run these before trusting any result, and report the findings.

- **Profile first** — missing rate per column, distinct counts and candidate keys, min/max/percentiles, and outlier flags (IQR or z-score). These findings shape every later query.
- **Normalize types** — string dates to dates, numeric strings to numbers. Silently coerced columns are the top source of wrong aggregates.
- **Deduplicate** — separate exact duplicate rows from business-key duplicates (the same order id appearing twice).
- **Handle outliers** — drop clear data errors, winsorize extremes, or keep and flag them. Always state which strategy was used.
- **Validate generated SQL** — run `EXPLAIN` to confirm the query touches the intended tables and columns and avoids accidental cross joins. A syntactically valid query can still be semantically wrong.
- **Declare data quality in the report** — end every report with missing rates, rows dropped or flagged, assumptions, and known limitations, so readers can judge how far to trust the numbers.

## Complete Example

User uploads `sales_2024.xlsx` (sheets: `Orders`, `Products`, `Customers`) and asks for top products by revenue plus monthly trends.

```
python scripts/analyze.py --files sales_2024.xlsx --action inspect

python scripts/analyze.py --files sales_2024.xlsx --action query \
  --sql "SELECT p.product_name, SUM(o.quantity * o.unit_price) AS total_revenue, SUM(o.quantity) AS total_units FROM Orders o JOIN Products p ON o.product_id = p.id GROUP BY p.product_name ORDER BY total_revenue DESC LIMIT 10"

python scripts/analyze.py --files sales_2024.xlsx --action query \
  --sql "SELECT DATE_TRUNC('month', order_date) AS month, SUM(quantity * unit_price) AS revenue FROM Orders GROUP BY month ORDER BY month" \
  --output-file ./outputs/monthly-trends.csv

python scripts/analyze.py --files sales_2024.xlsx --action summary --table Orders
```

## Notes

- Full DuckDB SQL is available: CTEs, subqueries, window functions, and advanced aggregations.
- Excel date columns are parsed automatically, so `DATE_TRUNC` and `EXTRACT` work directly.
- Files over 100 MB are handled without loading everything into memory.
- Columns with spaces are accessible using double quotes: `"Column Name"`.
- Cache lives in the system temp directory as `.data-analysis-cache/`; a changed file produces a new cache automatically.

## Best Practices

- Inspect before querying — never guess column names or types.
- Present results as plain tables in the reply, with the key takeaways explained in prose.
- Suggest follow-up analyses when a pattern is worth digging into.
- Export large result sets to a file instead of pasting thousands of rows into the conversation.
- Name output files after the analysis, not the input (`monthly-trends.csv`, not `output.csv`).

## Common Pitfalls

- **Guessing column names** — a sheet named `Sheet1` rarely has the columns you expect; inspect first.
- **Assuming all values are numeric** — currency and percentage columns are often stored as text with symbols.
- **Counting duplicates as separate rows** — business-key deduplication changes every aggregate.
- **Ignoring date parsing** — mixed date formats silently produce nulls in date functions.
- **Cross-file join on an unparsed key** — string ids with whitespace or type mismatches produce empty joins.
- **Dropping the data quality declaration** — numbers without caveats get trusted too far.

## Pairing with Other Tools

- `chart-visualization` — turn the aggregated result into a figure.
- `xlsx` / `spreadsheet` — write the export back into a workbook.
- `data-files` — convert between CSV, JSON, YAML, and TSV.
- `terminal` — runs the analysis script.
- `write-docs` / `doc-coauthoring` — turn the findings into a report.
