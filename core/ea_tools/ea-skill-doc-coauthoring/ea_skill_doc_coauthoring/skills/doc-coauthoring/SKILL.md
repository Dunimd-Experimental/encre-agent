---
name: doc-coauthoring
description: "Guide the user through a three-stage co-authoring workflow for docs, proposals, specs, PRDs, and RFCs: gather context, build the structure section by section, then test it with a fresh reader."
aliases:
  - doc-cowriting
  - proposal-writing
  - spec-writing
argument_hint: "[the document to co-author]"
user_invocable: true
hidden: true
context: inline
---

## Document Co-Authoring Workflow

A structured workflow for building a durable document together with the user. Act as an active guide across three stages: **Context Gathering**, **Refinement and Structure**, and **Reader Testing**.

## When to Use

- The user asks to write documentation, a proposal, a technical spec, a PRD, a design doc, an RFC, or a decision doc.
- The user starts a substantial writing task with real stakes.

## When NOT to Use

- **Writing API reference, README, changelog, or tutorial from source code** — use the `write-docs` skill.
- **Polishing prose in an existing draft** — use the `writing-guidelines` skill.
- **A short internal message, announcement, or status update** — use the `internal-comms` skill.
- **An email or newsletter** — use the `newsletter-generation` skill.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Draft and rewrite sections | `file_write` | `path`, `content` |
| Read referenced material | `file_read` | `path` |
| Fetch a linked page | `web_fetch` | `url` |
| Ask structured questions | `question` | numbered options |
| Search prior decisions | `grep` | `pattern`, `path` |
| Import into a shared doc | `notion` / `feishu` skill | only if installed and linked |

## How to Start

When the trigger fires, offer the workflow in one or two sentences, naming the three stages and the reason (a doc only works if it survives reading by someone who was not in the room). Ask whether they want the workflow or prefer to write freeform. If they decline, write freeform.

## Stage 1: Context Gathering

**Goal:** close the gap between what the user knows and what you know, so the guidance later is sharp instead of generic.

### Open with meta-questions

1. What type of document is this (spec, decision doc, proposal, RFC)?
2. Who is the primary audience?
3. What should happen when someone reads it?
4. Is there a template or format to follow?
5. Any constraints, dates, or politics that matter?

Tell them they can answer in shorthand or dump context in whatever order works.

### Collect the raw material

Ask for: background on the problem, related team discussions, why the alternatives were rejected, organizational context, timeline pressure, technical dependencies, and stakeholder concerns. Advise them not to organize it — just get it out. Offer the options: a stream-of-consciousness dump, links to channels or threads, or links to shared documents.

If they point to files, read them. If they point to a URL, use `web_fetch`. If they reference another integrated workspace (Notion, Feishu, email) and the corresponding skill is installed, offer to pull it in — then wait for confirmation before searching or reading anything outside the workspace.

### Ask clarifying questions

Once they signal the dump is done, ask 5-10 numbered questions that target the gaps. Tell them they can answer shorthand ("1: yes, 2: see the thread, 3: no because of backwards compat"), link more docs, or keep dumping.

**Exit condition:** you can ask about edge cases and tradeoffs without needing the basics explained. Confirm there is nothing more to add, then move on.

## Stage 2: Refinement and Structure

**Goal:** build the document section by section through brainstorming, curation, and surgical edits.

### Explain the loop

For each section: clarifying questions, then 5-20 options brainstormed, then the user keeps, removes, or combines, then a draft, then tight edits.

### Order the sections

Start with the section that has the most unknowns — usually the core decision or proposal — and work outward. If the user accepts the standard skeleton, use it:

1. Summary
2. Context and problem
3. Goals and non-goals
4. Proposal
5. Alternatives considered
6. Tradeoffs and risks
7. Open questions
8. Appendix

### Draft one section at a time

- Keep the section tight; if it is longer than about a screen, consider splitting it.
- Offer the section, note the assumptions you made, and ask what to change.
- Preserve the user's phrasing where it is good; do not rewrite for its own sake.
- Write to a real file as you go (`file_write`) so nothing is lost in the conversation.

### Keep decisions honest

Record rejected alternatives with one line on why. A doc that shows only the winning path cannot be argued with later.

## Stage 3: Reader Testing

**Goal:** catch blind spots before anyone else reads it, using a reader with no prior context.

### Simulate a cold read

Re-read the document assuming nothing: no project history, no meeting notes, no shared jargon. For each section, ask:

- What does this assume the reader already knows?
- Where would they have to guess?
- Which sentence would make them stop and reread?

### Check concrete failures

- **Undefined terms** — every acronym and internal name is defined on first use or spelled out once.
- **Broken referents** — "as discussed", "the usual approach", "our standard flow" all need an actual reference.
- **Orphan decisions** — every decision has a stated reason and a visible owner.
- **Missing counterarguments** — the strongest objection to the proposal is stated, not hidden.
- **Images without alt text** — an image in a shared doc is invisible to a reader or to a model summarizing it later; ask the user to provide descriptions.

### Report and fix

List the gaps as a short numbered list, patch the ones you can fix from context, and ask the user about the ones that need their call.

## Quality Bar

- The reader can act on the document without asking the author.
- Every claim about the world is either cited or flagged as an assumption.
- Alternatives and tradeoffs are on the page.
- Open questions are explicit rather than implied.
- The summary would survive being read alone.

## Best Practices

- Gather context in one focused pass, not across ten rounds of questions.
- Write the summary last even though it appears first — it is the hardest part and it improves once the body exists.
- Prefer concrete examples over abstractions; a reader believes one good example faster than three adjectives.
- Keep the doc under one sitting long; if it is longer, split it and link.
- Re-run Reader Testing after any substantive edit.

## Common Pitfalls

- **Skipping context gathering** — the first draft then reads like a generic template, and the user spends longer fixing it than co-authoring would have taken.
- **Writing all sections before reviewing any** — errors compound and the rewrite is large.
- **Silently filling gaps** — inventing facts to make a section feel complete is the most expensive mistake in this workflow.
- **Polishing too early** — structure problems cannot be fixed with wording.
- **Never testing with a cold read** — the author's context hides exactly the gaps the reader hits.
- **Letting the appendix grow without bounds** — move supporting material to linked files instead.

## Anti-patterns

- Dumping a full draft before asking a single question.
- Restructuring the doc five times while arguing about section names.
- Leaving "TBD" without an owner or a date.

## Pairing with Other Tools

- `write-docs` — technical documentation derived from source code.
- `writing-guidelines` — final prose and style pass.
- `internal-comms` — the announcement that goes out with the doc.
- `consulting-analysis` / `market-research` — evidence to back the proposal.
- `question` — structured clarification instead of open-ended pinging.
