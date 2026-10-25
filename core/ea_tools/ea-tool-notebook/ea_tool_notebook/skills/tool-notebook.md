---
name: tool-notebook
description: "Persistent Jupyter-style kernel: create/edit/execute cells, read outputs, inspect state, delete cells, or reset the session."
hidden: true
context: inline
---

# tool-notebook

`notebook` manages a stateful notebook session backed by a live kernel. Variables, imports and loaded data survive between cells, which is exactly what one-shot `python -c` runs cannot offer. Cells can be code or markdown, execution can target one cell or all of them, and outputs are inspectable after the fact.

## When to Use

- Iterative data exploration where state must persist across steps.
- Long computations you want to inspect and re-run partially.
- Plotting or tabular exploration before producing a final artifact.
- Loading a large dataset once, then querying it repeatedly.
- Walking through a computation as an ordered, re-runnable document.

## When NOT to Use

- One-shot scripts or CLI commands — use `terminal`.
- Editing and running repository test suites — use `test_run`.
- Persistent, versioned analysis artifacts — write a script file and run it.
- Very long jobs with no timeout: `execute_all` can block on one slow cell.
- Pure text generation or reasoning tasks.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `create_cell`, `edit_cell`, `execute_cell`, `execute_all`, `get_output`, `get_state`, `delete_cell`, `reset`. |
| `code` | string | no | Source (or markdown) for the cell; required by `create_cell` and `edit_cell`. |
| `cell_id` | string | no | Target cell; required by `edit_cell`, `execute_cell`, `get_output`, `delete_cell`. |
| `cell_type` | string | no | `code` (default) or `markdown`, for `create_cell`. |
| `timeout` | integer | no | Seconds; default 60 for `execute_cell`, 300 for `execute_all`. |
| `kernel_name` | string | no | Kernel to launch on `reset`; default `python3`. |

### Parameter Notes

- `cell_id` comes from the `create_cell` result or from `get_state`; you cannot address cells by index.
- `reset` **restarts the kernel and clears all cells** — it is destructive, not a soft clear.
- `timeout` is per call: `execute_all` uses one overall budget for the whole run.
- `cell_type` is only read on creation; `edit_cell` changes `code`, not the cell type.

## Example

```json
{
  "action": "execute_cell",
  "cell_id": "cell-3",
  "timeout": 120
}
```

## Best Practices

- Start with `get_state` in an existing session to see cells, kernel status and prior outputs.
- Keep cells small and single-purpose so a failure is easy to localize.
- Pin `timeout` deliberately for heavy cells instead of relying on the 60s default.
- Use markdown cells to record intent, so the notebook reads as an explanation.
- Fetch results with `get_output` rather than re-running a cell that already succeeded.

## Common Pitfalls

- `reset` wipes state and cells; there is no undo and no snapshot.
- A cell that hangs consumes the whole `execute_all` budget, leaving later cells unrun.
- Long output floods context — slice or summarize inside the cell.
- Kernel crashes (OOM, segfault) leave the session dead until `reset`.
- Working directory and environment inside the kernel may differ from the session cwd.

## Anti-patterns

- Using the notebook as a shell for one-off commands (`terminal` is cheaper).
- Accumulating dozens of exploratory cells instead of consolidating.
- Relying on kernel state for anything that must be reproducible.
- Running `execute_all` on a notebook with a known slow cell and no timeout raise.

## Pairing with Other Tools

- `terminal` — shell commands and scripts outside the kernel.
- `spreadsheet` / `chart` — turn computed results into deliverables.
- `file_write` — persist the final script or exported data.
- `test_run` — verify library code the notebook exercises.
