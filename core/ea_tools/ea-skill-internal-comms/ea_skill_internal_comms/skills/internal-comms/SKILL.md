---
name: internal-comms
description: "Write internal communications in the company's house format: 3P updates, company newsletters, FAQ answers, status reports, leadership updates, project updates, and incident reports."
aliases:
  - internal-comms-writing
  - company-comms
  - weekly-update
argument_hint: "[the update or communication to write]"
user_invocable: true
hidden: true
context: inline
---

## Internal Communications

You are writing an internal communication for: **{{args}}**

This skill ships four format guides, one per communication family. Pick the right one, read it, and follow it exactly — the value of a house format is that it is the same every time.

## When to Use

- 3P updates (Progress, Plans, Problems)
- Company or team newsletters
- FAQ responses
- Status reports
- Leadership updates
- Project updates
- Incident reports

## When NOT to Use

- **External newsletter or email digest for subscribers** — use the `newsletter-generation` skill.
- **Long-form documentation, proposals, or specs** — use the `write-docs` or `doc-coauthoring` skill.
- **A short one-off message to a chat channel** — just write it; a template is overhead.
- **Reviewing prose style of existing text** — use the `writing-guidelines` skill.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Read a format guide | `file_read` | `path` — sibling of this document |
| Draft the communication | `file_write` | `path`, `content` |
| Pull recent activity for the update | `git` | `action: log` |
| Gather metrics or status | `rest_client` | `url`, `method`, `headers` |
| Send it out | `email` | subject, to, body |

## Format Guides

| File | Use for |
|------|---------|
| `3p-updates.md` | Progress / Plans / Problems team updates |
| `company-newsletter.md` | Company-wide newsletters |
| `faq-answers.md` | Answering frequently asked questions |
| `general-comms.md` | Anything else that does not match the above |

## Workflow

### 1. Identify the communication type

Map the request to one of the guide files above. If the type is genuinely ambiguous (for example, a leadership update that reads like a status report), read `general-comms.md` first and then the nearest specific guide.

### 2. Load the guide

Read the chosen file in full before writing anything. It defines the section order, the tone, and what to gather.

### 3. Gather the content

Do not invent status. Pull the real material:

- `git log` for what actually shipped since the last update.
- Tickets, dashboards, or APIs via `rest_client` for numbers.
- Ask the user for anything only they know — decisions, blockers, dates.

### 4. Write in the house format

Follow the guide's section order and tone. Keep it scannable: short sentences, tight bullets, numbers where a number exists.

### 5. Check before sending

Confirm every date, owner, and number. An internal update with a wrong number is worse than no update.

## House Rules

- **Lead with the outcome.** The reader should know where things stand in the first two sentences.
- **Problems are content, not decoration.** Say the blocker plainly, with the owner and the date it was flagged.
- **One idea per bullet.** No compound bullets.
- **Numbers over adjectives.** "Down 40%" beats "down considerably".
- **Name the ask.** End with what you need from the reader, if anything.
- **Dates, always.** "Next week" is not a date.

## Examples

### 3P update

```
# Team Platform — Week 42

## Progress
- Search index rebuild landed in production (Mon). P95 query latency 410ms -> 180ms.
- Billing export now covers Q3 invoices; 14 legacy exports removed.

## Plans
- Rate limiter rollout Thu-Fri, canary at 10% then 50%.
- On-call handoff to infra team on Fri, docs handed over Wed.

## Problems
- Flaky retry test `test_task_retry_once` blocks the release train (owner: J. Lin, flagged Tue).
- Hiring: two open platform roles have been unfilled for 6 weeks.
```

### Status report

```
# Migration Status — 2026-10-24

| Workstream | State | ETA | Blocker |
|------------|-------|-----|---------|
| Auth service | Done | - | - |
| Billing | In progress, 68% | Oct 30 | Vendor schema change |
| Reporting | Not started | Nov 12 | Billing dependency |

Overall: on track for the Nov 15 cutover. Billing is the critical path.
```

### Incident report

```
# Incident 2026-10-22 — Payment callbacks delayed

**Severity:** SEV-2
**Duration:** 14:31 - 16:08 UTC (97 minutes)
**Impact:** 3,400 callback deliveries delayed by up to 90 minutes; no data loss.

## What happened
A retry queue backed up after a downstream timeout increase doubled retry intervals.

## Root cause
Timeout config was changed without a matching retry-budget update.

## Action items
1. Alert on queue depth above 5,000 (owner: A. Rivera, Oct 27)
2. Config change review for retry settings (owner: infra team, Oct 31)
```

## Common Pitfalls

- **Picking the wrong format** — a company newsletter is not a status report; read the guide first.
- **Inventing progress** — anything that is not in a log, a ticket, or the user's answer is not a fact.
- **Burying the problem** — a problem reported late is a decision lost.
- **Vague owners and dates** — "someone will look at it next week" is not an action.
- **Over-long updates** — if the reader cannot scan it in 60 seconds, cut it.
- **Ignoring the house format** — the format exists so readers can compare updates across weeks.

## Best Practices

- Reuse the previous issue as a template and update it, rather than rewriting from scratch.
- Keep the same section order every week so changes are visible.
- Put the one thing the reader must act on at the top.
- Archive old issues in a dated folder so the history stays searchable.

## Anti-patterns

- A "no update" status for work that is actually blocked.
- Bullet walls with no ordering and no owners.
- Celebrating milestones without stating the risk.

## Pairing with Other Tools

- `git` — the source of truth for what shipped.
- `rest_client` — pull live metrics and ticket state.
- `email` — send the finished update to the list.
- `write-docs` — when the update grows into durable documentation.
- `writing-guidelines` — final prose pass.
