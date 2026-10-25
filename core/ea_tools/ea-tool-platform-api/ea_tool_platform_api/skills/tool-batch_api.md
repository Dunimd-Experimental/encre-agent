---
name: tool-batch-api
description: "Create, retrieve or list asynchronous Batch API jobs that process a JSONL of requests offline at lower cost within a 24h window."
hidden: true
context: inline
---

# tool-batch_api

`batch_api` wraps the OpenAI-compatible Batch API: submit an uploaded JSONL file as a batch, poll one batch's status, or list all batches. Batches run asynchronously — typically minutes to hours, within a 24-hour completion window — and are billed at a discount, which makes them the right tool for thousands of independent requests.

## When to Use

- Process a large JSONL of chat-completion or embedding requests offline.
- Run evaluation sweeps, classification passes or bulk summarization.
- Do work where latency does not matter but cost does.
- Check progress or failure counts of a submitted batch.
- Enumerate recent batches before creating a duplicate.

## When NOT to Use

- A single interactive request — call the model directly.
- A handful of requests that finish in seconds — batching adds overhead.
- Workflow logic that needs results immediately to continue.
- Streaming or tool-calling loops that need multiple model round-trips.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `create`, `retrieve`, `list`. |
| `input_file_id` | string | no | `file_id` of the JSONL input (uploaded with `file_api`, `purpose="batch"`); required for `create`. |
| `endpoint` | string | no | Target endpoint per request, default `/v1/chat/completions`; also `/v1/embeddings`. Required for `create`. |
| `completion_window` | string | no | Maximum processing window, default `24h`; most backends support only `24h`. |
| `batch_id` | string | no | Batch to inspect; required for `retrieve`. |

### Parameter Notes

- `input_file_id` is a provider file ID, not a path — upload first with `file_api`.
- `endpoint` must match the request shape inside the JSONL; mismatches fail per-request, not at submit time.
- `list` takes no identifying parameter; it returns all batches on the account.
- There is no cancel action and no delete action.

## Example

```json
{
  "action": "create",
  "input_file_id": "file-abc123",
  "endpoint": "/v1/chat/completions",
  "completion_window": "24h"
}
```

## Best Practices

- Validate the JSONL locally (one request object per line, correct `custom_id`) before uploading.
- Poll `retrieve` a few times per window, not in a tight loop; batches are slow by design.
- Keep `custom_id` values stable and unique so results can be joined back to inputs.
- Record the `batch_id` immediately — it is the only handle for status and output.
- Start with a small pilot batch to confirm format and cost before a large run.

## Common Pitfalls

- Frequent polling wastes calls and tells you nothing new within minutes.
- A single malformed line can fail that request while the batch still "completes" — always inspect error counts.
- Output files must be downloaded separately via `file_api`; this tool returns metadata only.
- Expired input files (deleted, or past retention) make old batch IDs unusable.
- Backends differ in supported `endpoint` and `completion_window` values.

## Anti-patterns

- Submitting one request per batch.
- Blocking a conversation turn waiting for a 24h batch.
- Reusing a `custom_id` twice, making results ambiguous.

## Pairing with Other Tools

- `file_api` — upload the JSONL and later download the results file.
- `fine_tuning_api` — a different asynchronous job type with the same upload pattern.
- `json_tool` — inspect and validate request/response shapes.
- `notify` — alert when a batch finishes or fails.
