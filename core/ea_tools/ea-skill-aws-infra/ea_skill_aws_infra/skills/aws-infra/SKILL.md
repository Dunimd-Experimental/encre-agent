---
name: aws-infra
description: Inspect and audit AWS with the AWS CLI — resource inventory across EC2/S3/IAM/Lambda/ECS/EKS/RDS, health and CloudWatch triage, cost and security checks. Read-only by default; writes are proposed and run only after explicit confirmation.
---

# AWS Infrastructure

Answer operational questions about an AWS account by driving the local AWS CLI: what exists, what it costs, what is failing, and what would change. Everything is read-only unless the user asks for a change and confirms it.

## When to Use

- "What EC2 instances / S3 buckets / Lambda functions / RDS clusters do we have?"
- "Why is this service unhealthy?" — pull CloudWatch metrics, logs, and recent events.
- "Are any S3 buckets public, any security groups open to 0.0.0.0/0, any IAM keys older than 90 days?"
- "What did we spend last month, and on what?"
- Drafting a change (resize, delete, rotate credentials) where the exact CLI command must be shown before it runs.

## When NOT to Use

- Provisioning infrastructure as code — write Terraform/CDK/CloudFormation files with the `file_write` tool instead; this skill only runs ad-hoc CLI calls.
- Non-AWS clouds (GCP, Azure, Alibaba Cloud) — there is no equivalent skill; use `rest_client` against the provider API or the provider CLI through `terminal`.
- Reading local files or parsing logs already on disk — use `file_read` / `grep`.

## Prerequisites

- `aws` CLI on `PATH` (`aws --version`). If missing, say so instead of guessing resource state.
- Credentials resolvable from the environment, `AWS_PROFILE`, SSO cache, or `~/.aws/config`.
- Confirm the effective identity and region before answering anything: `aws sts get-caller-identity` plus the configured region. State both in the reply when results are region-scoped.

## Workflow

1. Establish context with `terminal`: `aws sts get-caller-identity`, then `aws configure list` to see profile/region resolution.
2. Pick the narrowest read command that answers the question (`list-*`, `describe-*`, `get-*`) and add `--query` / `--output json` so the output stays parseable.
3. For health questions, combine CloudWatch `get-metric-statistics` (trend) with `filter-log-events` or `tail` on the relevant log group (the specific error).
4. For cost questions, use Cost Explorer (`aws ce get-cost-and-usage`) with an explicit `--time-period` and `--granularity MONTHLY`; do not guess numbers.
5. If a change is requested, print the exact command, list what it affects, and wait for a clear "yes" before executing. Prefer `--dry-run` where the API supports it.

## Example

```shell
# Who am I and where am I operating?
aws sts get-caller-identity --output json
aws configure list

# Inventory running instances in one region
aws ec2 describe-instances \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,az:Placement.AvailabilityZone,name:Tags[?Key==`Name`]|[0].Value}' \
  --output table

# Anything public in S3?
aws s3api list-buckets --query 'Buckets[].Name' --output text
aws s3api get-public-access-block --bucket my-bucket

# Last month's spend by service
aws ce get-cost-and-usage \
  --time-period Start=2026-09-01,End=2026-10-01 \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```

```shell
# Proposed change: show it, then wait for confirmation
aws ec2 stop-instances --instance-ids i-0123456789abcdef0 --dry-run
```

## Best Practices

- Always report the account ID, profile, and region you queried; multi-account setups make silent assumptions dangerous.
- Prefer `--query` over piping into `grep` — it keeps JSON structure and avoids matching unrelated text.
- Summarise instead of dumping: table of counts plus the 5–10 rows that matter, with the raw command available on request.
- Paginate explicitly with `--max-items` / `--starting-token` for large accounts; default pages hide resources.
- Use `--no-cli-pager` in non-interactive shells so output is not swallowed.

## Common Pitfalls

- Running without credentials and interpreting an auth error as "no resources exist".
- Forgetting that most `describe-*` calls are region-scoped, then reporting a partial inventory as complete.
- `aws s3 ls` and `aws s3api list-objects-v2` behave differently on permissions and pagination; choose deliberately.
- CloudWatch metrics have a delay (1–5 minutes for basic metrics) — a spike "missing" from the last minute is normal.
- Cost Explorer data lags by up to 24 hours and needs `ce:GetCostAndUsage` permission.

## Anti-patterns

- Executing destructive commands (`terminate-instances`, `delete-bucket`, `delete-user`, `modify-db-instance`) because the user's request sounded like an instruction. Always show and confirm.
- Echoing access keys, session tokens, or secret values into the transcript.
- Looping over every region to "be thorough" without saying so — it is slow and multiplies permission errors.
- Inventing resource IDs, ARNs, or costs that were never returned by a command.

## References

- `references/aws-cli-queries.md` — ready-made query patterns for EC2, S3, IAM, Lambda, RDS, ECS, CloudWatch, and billing.
