---
name: tool-cloud-storage
description: "One interface for S3, GCS, and Azure Blob: list, upload, download, delete, inspect objects, sync directories."
hidden: true
context: inline
---

# tool-cloud-storage

Wraps the three major object stores behind a single action set. Credentials are resolved from the environment by default, and responses come back parsed instead of as CLI text. Provider capabilities differ — see the matrix below before choosing an action.

## When to Use

- Moving artifacts (builds, archives, datasets, model files) to or from a bucket.
- Listing what exists under a prefix before deciding what to fetch.
- Checking object metadata (size, timestamp, storage class) without downloading.
- Syncing a local directory up to S3 as a batch.
- You want to avoid shelling out to `aws`, `gsutil`, or `az` and parsing their output.

## When NOT to Use

- Files in the user's DingTalk/enterprise drive — use the drive/document tools, not object storage.
- Small local file moves within one machine — plain filesystem tools.
- Generating a presigned URL for a third party — not exposed here; use `terminal` with the provider CLI.
- Bucket-level administration (IAM, lifecycle, versioning, replication) — this tool is object-level only.
- Streaming large datasets that need range reads/parallel multipart — download once or use a provider SDK in a `notebook`.

## Key Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `provider` | string enum | no | `s3` (default), `gcs`, `azure`. |
| `action` | string | yes | `list`, `upload`, `download`, `delete`, `info`, `list_buckets`, `sync_up`. Availability varies by provider. |
| `bucket` | string | yes | S3 bucket, GCS bucket, or Azure container name. |
| `key` | string | yes for `download`/`delete`/`info` | Object key. Optional for `upload` (defaults to the local filename). |
| `local_path` | string | yes for `upload`/`sync_up` | Local file (or directory for `sync_up`). Optional for `download` (defaults to the key's basename). |
| `prefix` | string | no | Prefix filter for `list`. |
| `region` | string | no | AWS region (default `us-east-1` or `AWS_REGION` / `AWS_DEFAULT_REGION`). |
| `access_key` | string | no | AWS access key id; overrides `AWS_ACCESS_KEY_ID`. |
| `secret_key` | string | no | AWS secret access key; overrides `AWS_SECRET_ACCESS_KEY`. |
| `credentials_path` | string | no | GCS service-account JSON; overrides `GOOGLE_APPLICATION_CREDENTIALS`. |
| `connection_string` | string | no | Azure Storage connection string; overrides `AZURE_STORAGE_CONNECTION_STRING`. |
| `public` | boolean | no | `upload`: make the object publicly readable. |
| `max_keys` | integer | no | `list`: max results (default 100, max 1000). |

### Provider Capability Matrix

| Action | S3 | GCS | Azure |
|---|---|---|---|
| `list` | yes | yes | yes |
| `upload` | yes | yes | yes |
| `download` | yes | yes | yes |
| `delete` | yes | yes | yes |
| `info` | yes | yes | yes |
| `list_buckets` | yes | yes | no |
| `sync_up` | yes | no | no |

### Parameter Notes

- `key` is the full object path including any folder-like prefixes (`reports/2026/q3.csv`). There are no real directories.
- `upload` without `key` uses the local filename, which is rarely what you want for organised buckets — pass `key` explicitly.
- `download` without `local_path` writes next to the current working directory using the key's basename; pass a path to control it.
- Sequential `list` calls paginate with `max_keys`; a large bucket needs prefix scoping, not a bigger limit.
- `public: true` is irreversible in effect for that object (it stays readable until ACL change) — use it deliberately.
- Inline credentials are a last resort; prefer env vars or the provider's standard credentials file.

## Example

```json
{
  "provider": "s3",
  "action": "upload",
  "bucket": "encre-artifacts",
  "key": "desktop/0.5.0/encre-desktop-win.zip",
  "local_path": "dist/encre-desktop-win.zip",
  "region": "ap-east-1"
}
```

Scoped listing:

```json
{
  "provider": "s3",
  "action": "list",
  "bucket": "encre-artifacts",
  "prefix": "desktop/0.5.0/",
  "max_keys": 200
}
```

Download to a chosen path:

```json
{
  "provider": "gcs",
  "action": "download",
  "bucket": "encre-research",
  "key": "benchmarks/runners.jsonl",
  "local_path": "backend/benchmarks/runners.remote.jsonl"
}
```

## Best Practices

- Always scope `list` with `prefix` on anything but a tiny bucket; unscoped listings are slow and huge.
- Verify with `info` before downloading a large object — size and timestamp tell you whether it is the artifact you think it is.
- Upload with an explicit `key` that encodes version/date so objects are immutable and traceable.
- Keep credentials in env vars (`AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`, `AZURE_STORAGE_CONNECTION_STRING`) so nothing secret lands in the transcript.
- After `upload`, do a `list`/`info` on the key to confirm the size matches the local file.
- For directory sync, check the target prefix first and remember `sync_up` does not delete remote extras by default.

## Common Pitfalls

- Wrong region for S3 — a bucket in another region yields redirect/permission errors that look like auth failures.
- Azure has no `list_buckets`; calling it returns an unsupported-action error, not an empty list.
- Leading slashes in `key` (`/reports/x.csv`) create an object whose name starts with `/`, invisible to normal prefix listings.
- Assuming `sync_up` mirrors deletions — it uploads, it does not prune.
- Passing `local_path` for `download` to a directory that does not exist; create the parent first.
- Confusing bucket (container) with key: swapping them produces a "no such bucket" error that is easy to misread.

## Anti-patterns

- Shelling out to `aws s3 cp` and scraping stdout when this tool returns structured results.
- Putting `secret_key`/`connection_string` in a prompt that will be logged, when env vars already work.
- Downloading an entire prefix "just in case"; list, choose, then download.
- Using object storage as a scratchpad for intermediate files that never need to leave the machine.

## Pairing with Other Tools

- `archive` — package a tree, then upload the single artifact.
- `file_read` / `file_write` — inspect or prepare the local side of a transfer.
- `hash_crypto` — checksum before/after transfer to prove integrity.
- `database` / `notebook` — consume downloaded datasets.
- `terminal` — for provider features this tool does not expose (presigned URLs, ACLs, multipart tuning).
