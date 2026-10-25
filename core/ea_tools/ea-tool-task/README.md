# ea-tool-task

Unified task tool for the Encre Agent task system.

## Description

This plugin provides the `task` tool, which consolidates the entire background
task lifecycle into a single unified tool with an `action` parameter:

- **create** — spawn a new background sub-task (bash, agent, or workflow)
- **list** — enumerate all tasks with optional status filter
- **get** — one-shot snapshot of a single task's state
- **output** — stream task output with optional blocking wait
- **stop** — cancel a running task (idempotent)
- **update** — low-level mutation of status/result/error

## Actions

| Action   | Required Params | Description |
|----------|-----------------|-------------|
| `create` | `name`, `prompt` | Create a new background task |
| `list`   | —               | List all tasks (optional `status` filter) |
| `get`    | `task_id`       | Get task details (result truncated to 500 chars) |
| `output` | `task_id`       | Get task output (result up to 5000 chars, can block) |
| `stop`   | `task_id`       | Cancel a running task |
| `update` | `task_id`       | Update task status/result/error |

## Example

```json
{
  "action": "create",
  "name": "Run tests",
  "task_type": "bash",
  "prompt": "pytest --tb=short"
}
```

```json
{
  "action": "output",
  "task_id": "task-123",
  "block": true,
  "timeout": 60000
}
```

## Notes

- Previously split into 6 separate packages (ea-tool-task-create, etc.) — merged to prevent logical impossibilities (e.g. creating tasks but never stopping them)
- Uses the `manage` tool pattern: single tool with `action` enum parameter
- Task IDs are returned by `create` and can be used with all other actions
