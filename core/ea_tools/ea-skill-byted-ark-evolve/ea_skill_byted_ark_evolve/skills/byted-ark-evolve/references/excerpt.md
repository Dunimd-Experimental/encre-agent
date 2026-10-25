
## Gene Integration (v0.3.1, local static library)

Gene is a **standardized template library for mutations**. This version ships with the skill package as a static library `references/gene-library.json`, **no cloud calls**.

Boundaries:
- Gene **is not an analyzer**, does not handle root cause attribution
- Gene **is not an executor**, does not directly modify any files
- Gene **is not a local rule matcher**, local scripts are not responsible for gene relevance determination

Division of responsibilities:
- **Agent / subagent**: Read signals, logs, trajectories, complete attribution analysis
- **Gene static library**: Provide mutation template candidates (id / summary / pattern_key / rule_text)
- **Evolution skill**: Orchestrate load → Agent judges gene candidates → generate mutation → user confirmation → execute

### When to Call Gene

The following scenarios must enter the Gene flow:
1. After `/evolve` manual trigger, after Step 2 attribution is complete, enter Step 2.5 Gene candidate screening
2. After auto-evolution trigger, before mutation design, enter Step 2.5
3. When the user says "remember this pattern / do this again next time", first browse the gene library to determine if there is already a reusable template

### Gene Commands

```shell
python skills/byted-ark-evolve/scripts/gene.py --list
python skills/byted-ark-evolve/scripts/gene.py --show <gene_id>
python skills/byted-ark-evolve/scripts/gene.py --summary
```

Rules:
- Library is released with the skill version (update requires skill version upgrade), no `--fetch` command
- Agent reads signal + attribution + gene content in Step 2.5, judges "which gene's solutions, if applied in advance, could prevent the current negative feedback or error from occurring again"
- In Step 3, Agent generates mutation proposal based on the selected gene template

Detailed flow (Step 0-7, including Step 2.5 Gene candidate screening): Read `references/evolution-steps.md`

Summary: Snapshot Diff → Scan Signals → **Trajectory Clustering (Skill Candidate Detection)** → Attribution Analysis → **Gene Candidate Screening (Static Library + Agent Judgment)** → Design Mutation → **User Confirmation** → **Assemble JSON + Render Report/Dashboard** → **Output Evolution Summary** → Mark Processed → Update Snapshot

### Trajectory → Skill Emergence (Step 1.5)

During evolution analysis, automatically detect extractable patterns in trajectories:
- ≥3 similar Golden trajectories → Suggest extracting as an independent skill
- ≥2 Correction trajectories with the same cause → Suggest adding protection rules

Flow: Detect → Present candidates to user → User confirmation → Call **skill-creator** to generate.
If the user does not have skill-creator installed, prompt installation.

### User Confirmation Interaction Standards

After designing mutations, **must present the proposal to the user for confirmation first; do not execute any writes before confirmation**.

Display format (list each item):

```
### Evolution Proposal (N changes total)

1. 📝 [AGENTS.md] — Add "Chinese Coding Standards"
   - Layer attribution: Protocol
   - Change type: Append (user-owned file, append only)
   - Pareto check: ✅ Passed
   - Summary: Add a new section at the end specifying Chinese API calls use Python

2. 📝 [SOUL.md] — Modify "Task Execution Flow"
   - Layer attribution: Identity
   - Change type: Modify (evolvable file)
   - Pareto check: ✅ Passed
   - Before: "Directly execute user instructions"
   - After:  "First retrieve relevant corrections, then execute"

Do you want to execute the above N changes? You can:
- Accept all
- Reject all
- Select individually (e.g., "accept 1, reject 2")
```

Key rules:
- Each mutation must list the target file, change type, and Pareto result
- user-owned files are specifically marked "append only"
- Do not perform any write operations before the user explicitly responds
- Rejected mutations are marked as `rejected`, records preserved

### Evolution Completion Summary

After the report is generated, directly output a **natural language summary** in the conversation (not file paths, but a summary the user can understand directly):

```
This evolution processed X feedback items, generating Y pending proposals/changes:
- [AGENTS.md] Added "Chinese Coding Standards" — because you mentioned 4 times not to use curl to send Chinese
- [SOUL.md] Adjusted task execution flow — added step to retrieve correction records before execution

Rejected: W items
Full report: evolution-data/reports/evolution-<date>.html

Next step: When you encounter related scenarios again, I will observe whether these improvements take effect.
```

Rules:
- Use natural language, no jargon (do not say mutation / signal / layer)
- Each change explains "what changed" + "why changed"
- Inform the report path, but do not make the path the primary output
- Mention subsequent verification plan

When attributing, refer to `references/layer-model.md` + `evolution-data/file-registry.json`
When writing, refer to `write_policy` in `file-registry.json` (evolvable→section-edit / user-owned→append-only)
Pareto check refers to `references/pareto-rules.md`

### Report Generation Flow (Template Approach)

1. Assemble report data as JSON (refer to `references/report-schema-example.json`)
2. Write to `evolution-data/reports/evolution-<date>.json`
3. Call `python scripts/report-render.py <json> --output <html>` to render HTML
4. (Optional) Assemble Dashboard JSON (refer to `references/dashboard-schema-example.json`) → `dashboard-render.py`

Agent is only responsible for filling in the pure data JSON; Python scripts handle all conditional rendering logic (e.g., whether there are rejected changes, saturation, high severity signal display, etc.).

---

## Pending Proposal Lifecycle (v0.2.2)

`pending-evolution.json` is positioned as **a queue of pending proposals for human reading**, not a "next morning exclusive reminder".

Rules:
- Nightly timer review only generates proposals, does not auto-apply
- As long as the proposal is still valid and has not been processed by the user, any subsequent `session_start` can display it again
- Display status and decision status are separated: `pending -> presented -> accepted/rejected/stale/superseded`
- `pending-evolution.json` / `daily-digest.json` are export layers; **DB is the primary state source**

Recommended commands:

```shell
python skills/byted-ark-evolve/scripts/pending-evolution.py export
python skills/byted-ark-evolve/scripts/pending-evolution.py show
python skills/byted-ark-evolve/scripts/pending-evolution.py present --group <proposal_group_id>
python skills/byted-ark-evolve/scripts/pending-evolution.py decide --group <proposal_group_id> --decision accepted
python skills/byted-ark-evolve/scripts/pending-evolution.py stale --older-than-hours 72
```

## 5. Verification

Verification status: Pending → Observed → Verified / Relapsed / Partially Effective

Status descriptions:
- **Pending**: Just proposed, waiting to be verified in real scenarios
- **Observed**: Preliminary pass once, but not fully reliable
- **Verified**: ≥3 times across sessions without relapse, behavior change confirmed
- **Relapsed**: Violated again after verification, needs reinforcement
- **Partially Effective**: Effective in some scenarios, not covered in others

Detailed rules and credit weights: Read the verification standards section of `references/pareto-rules.md`

---

## 6. Quick Commands

| User Says | Agent Does |
|--------|---------|
| `/evolve` | Execute `orchestrator.py timer-review --skip-gate --worker-mode agent`, full 7-step evolution (≥1 signal is sufficient, not subject to Gate threshold and cooldown) |
| `/evolve init` | Execute on first install: initialization scan + generate file registry |
| `/evolve scan` | Scan historical conversations, extract existing feedback signals |
| `/evolve detect` | Batch detect signal candidates in historical conversations (`scan-history.py detect`) |
| `/evolve dashboard` | Generate and open Dashboard (`dashboard-render.py` → `evolution-data/dashboard.html`) |
| `/evolve help` | Display onboarding guidance (read `references/onboarding.md`) |
| "Remember this pattern" | Record positive signal as positive reference for future evolution analysis |
| "Don't do this again" | Record correction signal, mark behavior needing improvement |
| "Evolution status" | Quick view: `query.py dashboard` (text); full page: assemble JSON → `dashboard-render.py` (HTML) |
| "Gene list / gene overview" | Call `gene.py --list` / `gene.py --summary` |
| "Evolution report" | Assemble JSON (refer to `references/report-schema-example.json`) → `report-render.py` |

---

## 7. Query Tools

```shell
python skills/byted-ark-evolve/scripts/query.py dashboard          # Dashboard
python skills/byted-ark-evolve/scripts/query.py signals --unprocessed  # Unprocessed signals
python skills/byted-ark-evolve/scripts/query.py signals --layer protocol  # By layer
python skills/byted-ark-evolve/scripts/query.py mutations --status pending  # Mutations
python skills/byted-ark-evolve/scripts/query.py mutations --source user-direct  # User-direct changes
python skills/byted-ark-evolve/scripts/query.py trajectories       # Trajectory statistics
python skills/byted-ark-evolve/scripts/query.py gene-matches       # Latest gene candidate screening results

# Signal candidate detection (new in v0.2.9)
python skills/byted-ark-evolve/scripts/scan-history.py detect --days 7            # Detect signal candidates in recent 7 days of conversations
python skills/byted-ark-evolve/scripts/scan-history.py detect --min-confidence high  # Only high confidence candidates
```


## Apply Loop (v0.2.4)

**After the user accepts the proposal, it is recommended to complete the application via the following command to ensure git commit / snapshot / dashboard are synchronously updated.**

```shell
python skills/byted-ark-evolve/scripts/apply-proposal.py --group <proposal_group_id>
```

This command automatically completes the full loop:
1. Apply the mutations under this group to the target files
2. Execute git commit (one commit per proposal_group)
3. Call snapshot save
4. Write back review/result to DB
5. Regenerate dashboard.html

Can also be called via orchestrator (same effect):
```shell
python skills/byted-ark-evolve/scripts/orchestrator.py apply-proposal --group <proposal_group_id>
```

## Dashboard Fixed Template (v0.2.5+)

Dashboard uses a **template and data separation** architecture, ensuring styles are fixed and unaffected by the model.

### Architecture

| File | Responsibility | Update Method |
|------|------|----------|
| `scripts/dashboard-template.html` | Fixed HTML/CSS/JS template | Only manually iterated by developer |
| `scripts/dashboard-render.py` | Query DB + references/gene-library.json → JSON → inject into template | Auto-called by orchestrator |
| `evolution-data/dashboard.html` | Final output, opened in user's browser | Auto-generated each pipeline run |

### Usage

```shell
# Manual generation
python skills/byted-ark-evolve/scripts/dashboard-render.py --db evolution-data/evolution.db --data-dir evolution-data/

# Auto-generation (auto-triggered after timer-review / apply-proposal completes)
```

### Three Tabs

- **Overview**: Global statistics, trend charts, change verification, layer distribution
- **Activity**: Date picker + single evolution report details (source feedback, change diff, status)
- **Genes**: Gene table + accordion expand related changes
