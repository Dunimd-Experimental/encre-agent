---
name: tool-fine-tuning-api
description: "Start a fine-tuning job from an uploaded JSONL training file, or list existing jobs and their statuses."
hidden: true
context: inline
---

# tool-fine_tuning_api

`fine_tuning_api` covers the provider-side fine-tuning lifecycle it exposes: `create` a job from a training file that has already been uploaded, and `list` jobs with their statuses. There is no retrieve-by-id, cancel or delete action here. Jobs are slow (hours), expensive, and the resulting model is a new provider artifact.

## When to Use

- You have curated, validated JSONL training data and want a domain-specialized model.
- You need to check whether previous jobs succeeded, failed or are still running.
- You are auditing which base models have been fine-tuned on this account.
- A repeated prompt-engineering pattern has plateaued and supervised tuning is the next step.

## When NOT to Use

- Quick experimentation — try prompting, few-shot examples or retrieval first.
- Data preparation or upload — use `json_tool` to validate and `file_api` to upload.
- Inference with the resulting model — call the model normally.
- Cancelling or inspecting a single job — unsupported here; use `rest_client` against the provider API.

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `create` (new job) or `list` (all jobs and statuses). |
| `training_file` | string | no | `file_id` from `file_api` with `purpose="fine-tune"`; required for `create`. |
| `model` | string | no | Base model to tune (e.g. `gpt-4o`, `gpt-3.5-turbo`); required for `create`. |

### Parameter Notes

- `training_file` is an uploaded file ID, never a local path.
- `model` must be a base model the backend permits tuning; instruct-tuned or already-tuned models often are not.
- `list` ignores both parameters.
- There is no validation-file, suffix, hyperparameter or epochs parameter in this wrapper.

## Example

```json
{
  "action": "create",
  "training_file": "file-train-789",
  "model": "gpt-4o-mini"
}
```

## Best Practices

- Validate the JSONL schema locally (roles, message ordering, no trailing commas) before uploading.
- Start with a small, high-quality dataset; thousands of noisy examples underperform hundreds of clean ones.
- Check `list` before creating to avoid duplicate jobs burning quota.
- Decide the evaluation set *before* training, and keep it out of the training file.
- Record the job ID and base model; you will need them to call the tuned model later.

## Common Pitfalls

- Wrong or missing `purpose="fine-tune"` on upload causes the job to be rejected.
- A job can fail mid-run; `list` shows status but not a detailed per-example error.
- Costs accrue for training tokens, and the tuned model may cost more per call than the base.
- Fine-tuning is irreversible in the sense that you cannot "undo" a job — you abandon the model.
- Backends that do not implement these methods return an unsupported error.

## Anti-patterns

- Fine-tuning to teach facts that retrieval would handle better.
- Submitting unvalidated JSONL and discovering the format error hours later.
- Tuning before establishing a baseline with the base model.
- Treating fine-tuning as a fix for a badly designed prompt.

## Pairing with Other Tools

- `file_api` — upload the training file, manage its lifecycle.
- `json_tool` — validate and inspect training data structure.
- `batch_api` — run bulk evaluations against base and tuned models.
- `notebook` — build and inspect the dataset before training.
