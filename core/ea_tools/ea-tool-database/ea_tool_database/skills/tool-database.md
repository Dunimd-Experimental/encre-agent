---
name: tool-database
description: "Run SQL and NoSQL operations across SQLite, Postgres, MySQL, MSSQL, Oracle, DuckDB, ClickHouse, MongoDB and Redis: query, execute, inspect schemas, export, or manage pools."
hidden: true
context: inline
---

# tool-database

`database` is a single entry point to many engines. `action` selects the operation family — SQL `query`/`execute`, schema inspection (`tables`, `views`, `indexes`, `describe`), plans and sizes, exporting, connection management, or engine-specific `mongodb` / `redis` sub-actions. The connection comes from `database_url`; without it you get an in-memory SQLite database, which is useful for throwaway work.

## When to Use

- Explore an unfamiliar schema: `tables` → `describe` → sample `query`.
- Run a read query with bound parameters and a row cap.
- Apply DML/DDL deliberately with `execute`.
- Understand performance: `explain` for a plan, `size` for table growth.
- Export a result set as JSON/CSV/markdown for a report.
- Drive MongoDB or Redis directly via their dedicated actions.
- Probe connectivity with `ping` before a longer session.

## When NOT to Use

- Application ORM calls or migrations between environments — out of scope.
- File-based analysis of CSVs/Excel — use `spreadsheet` or `notebook`.
- Bulk ETL or heavy analytics better served by a script in `terminal`.
- Vector/semantic retrieval — use `create_embeddings` plus an index.
- Any destructive `DROP`/`GRANT`/`ALTER SYSTEM` — blocked by design.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | no | `query` (default), `execute`, `tables`, `views`, `indexes`, `describe`, `explain`, `size`, `export`, `metadata`, `ping`, `close_pool`, `close_all`, `list_databases`, `create_database`, `mongodb`, `redis`. |
| `sql` | string | no | SQL text; required for `query`, `execute`, `explain`, `export`. |
| `params` | array | no | Positional values bound to `?` placeholders — prefer over interpolation. |
| `database_url` | string | no | Connection URL (`sqlite:///path`, `postgresql://`, `mysql://`, `mssql://`, `oracle://`, `duckdb://`, `clickhouse://`, `mongodb://`, `redis://`); defaults to in-memory SQLite. |
| `table` | string | no | Target table for `describe`, `indexes`, `size`. |
| `new_database` | string | no | Database name for `create_database`. |
| `limit` | integer | no | Max rows per result set; default 100. |
| `read_only` | boolean | no | Restrict to SELECT/PRAGMA/EXPLAIN/SHOW/DESCRIBE/WITH; default false. |
| `timeout` | integer | no | Per-query timeout in seconds; default 30. |
| `export_format` | string | no | `json`, `csv`, `markdown` for `export`. |
| `meta_action` | string | no | Engine-specific metadata sub-action for `metadata` (varies by engine). |
| `mongodb_action` | string | no | Mongo operation: `find`, `find_one`, `insert`, `update`, `delete`, `aggregate`, `collections`, `count`, `distinct`, `indexes`, `bulk_write`, `watch`, and more. |
| `mongodb_db` | string | no | Mongo database; falls back to the URL's database segment. |
| `mongodb_collection` | string | no | Collection to operate on. |
| `mongodb_filter` | string | no | Filter as a JSON string, e.g. `{"status":"active"}`. |
| `mongodb_document` | string | no | Document(s) to insert, as JSON. |
| `mongodb_update` | string | no | Update operators, e.g. `{"$set":{...}}`. |
| `mongodb_replacement` | string | no | Replacement document for `find_one_and_replace`. |
| `mongodb_multi` | boolean | no | Update/delete multiple documents; default false (delete defaults to true). |
| `mongodb_pipeline` | string | no | Aggregation pipeline as a JSON array. |
| `mongodb_field` | string | no | Field for `distinct`. |
| `mongodb_sort` | string | no | Sort spec as JSON list, e.g. `[["field",1]]`. |
| `mongodb_projection` | string | no | Projection as JSON object. |
| `mongodb_skip` | integer | no | Documents to skip; default 0. |
| `mongodb_return_document` | string | no | `before` or `after` for update/replace; default `after`. |
| `mongodb_new_name` | string | no | New collection name for `rename_collection`. |
| `mongodb_keys` | string | no | Index keys as JSON list. |
| `mongodb_index_name` | string | no | Index to create or drop. |
| `mongodb_unique` | boolean | no | Create a unique index; default false. |
| `mongodb_operations` | string | no | Bulk-write descriptors as a JSON array. |
| `redis_command` | string | no | Redis command, e.g. `GET`, `SET`, `KEYS`, `HSET`, `SCAN`, `INFO`. |
| `redis_args` | array | no | Positional arguments for the Redis command. |

### Parameter Notes

- `action` defaults to `query`; for Mongo/Redis you must set `action="mongodb"` / `"redis"` **and** the corresponding sub-action field.
- `params` binds `?` placeholders positionally — never interpolate user input into `sql`.
- `read_only=true` is the safety switch for exploratory sessions; it rejects anything but SELECT/PRAGMA/EXPLAIN/SHOW/DESCRIBE/WITH.
- `limit` caps rows per result set, not total work; a slow query still runs to `timeout`.
- Mongo values (`filter`, `document`, `pipeline`, `keys`) are **JSON strings**, not objects.
- `meta_action` names differ per engine; unknown values fail rather than defaulting.

## Example

```json
{
  "action": "query",
  "database_url": "postgresql://analyst@db.internal:5432/metrics",
  "sql": "SELECT day, revenue FROM daily WHERE day >= ? ORDER BY day DESC",
  "params": ["2026-09-01"],
  "limit": 30,
  "read_only": true
}
```

## Best Practices

- Start with `tables` and `describe` instead of guessing column names.
- Always bind through `params`; string interpolation invites injection and quoting bugs.
- Set `read_only=true` for any exploratory session you did not intend to mutate.
- Keep `limit` small while iterating, then raise it for the final export.
- Use `explain` before optimizing a query rather than after it times out.
- `close_pool` when you are done with a long-lived connection.

## Common Pitfalls

- DDL like `DROP DATABASE`, `GRANT`/`REVOKE` and `ALTER SYSTEM` is blocked — the tool refuses rather than warns.
- Queries are cancelled at `timeout` (30s default); long analytical scans need a higher value.
- Driver packages must be installed for non-SQLite engines, or the connection fails.
- MongoDB parameters are JSON strings; passing a real object breaks parsing.
- Omitting `database_url` silently targets an in-memory SQLite DB, so "no tables" is expected.
- `limit` truncation can hide that a query returns far more rows than expected.

## Anti-patterns

- Building SQL by concatenating user input.
- Running UPDATE/DELETE without a WHERE clause "to test".
- Using `database` for CSV analysis (`spreadsheet` / `notebook` fit better).
- Leaving pools open across unrelated tasks.

## Pairing with Other Tools

- `spreadsheet` / `chart` — present exported rows.
- `notebook` — heavier analysis over query results.
- `env_manager` — manage connection strings and secrets.
- `json_tool` — inspect exported or Mongo JSON payloads.
