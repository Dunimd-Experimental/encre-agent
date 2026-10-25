---
name: tool-file-api
description: "Upload, list, retrieve or delete files through the backend's OpenAI-compatible Files API, returning file IDs used by batch and fine-tuning."
hidden: true
context: inline
---

# tool-file_api

`file_api` manages files that live on the model provider rather than on disk: it uploads a local file and returns a `file_id`, enumerates previously uploaded files, fetches metadata for one, and deletes by ID. Those IDs are the currency of the provider's downstream endpoints — Batch jobs and fine-tuning runs both take a `file_id`, not a path.

## When to Use

- Stage a JSONL request file for a Batch job: `action="upload"` with `purpose="batch"`.
- Stage training data for fine-tuning: `purpose="fine-tune"`.
- Find an existing upload's ID before creating a job: `action="list"`.
- Confirm size/status of an upload: `action="retrieve"`.
- Clean up obsolete uploads to stay under quota: `action="delete"`.

## When NOT to Use

- Reading or writing local files — use `file_read` / `file_write`.
- General HTTP uploads to arbitrary endpoints — use `rest_client`.
- Attaching an image to a chat turn — pass it directly to the model, not through Files.
- Managing documents in the workspace (docx, pptx, xlsx) — use the document-family tools.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `upload`, `list`, `retrieve`, `delete`. |
| `file` | string | no | Absolute local path (or base64 data) of the file to upload; required for `upload`. |
| `file_id` | string | no | Target file ID for `retrieve` / `delete`. |
| `purpose` | string | no | `assistants` (default), `batch`, `fine-tune`, `vision`; must match the downstream endpoint. |

### Parameter Notes

- Each action reads only its own parameters; supplying `file` for `retrieve` does nothing.
- `purpose` is fixed at upload time and cannot be changed later — re-upload to change it.
- `file_id` values are opaque provider IDs, valid only against the same backend and account.

## Example

```json
{
  "action": "upload",
  "file": "D:/work/batch-requests.jsonl",
  "purpose": "batch"
}
```

## Best Practices

- Use an absolute path for `file`; relative paths are ambiguous across sessions.
- Set `purpose` before uploading: `batch` for Batch API, `fine-tune` for training data, `assistants` otherwise.
- Record the returned `file_id` in your notes or the next call's arguments — it is the only handle.
- Validate the JSONL locally (`json_tool`, `terminal`) before uploading; a malformed file wastes an upload and a job.
- Delete stale uploads to keep quota headroom.

## Common Pitfalls

- Backends without Files API support error out entirely; check the provider first.
- Per-file size limits apply and vary by backend.
- Re-uploading the same content creates a new ID; it does not deduplicate.
- Deleting a file that a running batch or fine-tune job references can break that job.
- A wrong `purpose` is not rejected at upload time but fails downstream.

## Anti-patterns

- Uploading a local file just to read it back.
- Using `file_api` as a general object store for workspace assets.
- Re-uploading a training file for every job instead of reusing the ID.

## Pairing with Other Tools

- `batch_api` — create a batch from the uploaded `file_id`.
- `fine_tuning_api` — start a job from the training `file_id`.
- `json_tool` / `terminal` — validate JSONL before upload.
- `rest_client` — provider endpoints not covered by these wrappers.
