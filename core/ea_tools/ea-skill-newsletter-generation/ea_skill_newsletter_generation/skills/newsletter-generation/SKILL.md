---
name: newsletter-generation
description: "Generate researched newsletters, email digests, weekly roundups, and industry briefings: curate multiple sources, add original commentary, and format for email or web."
aliases:
  - newsletter
  - weekly-digest
  - industry-briefing
argument_hint: "[topic, format, and audience]"
user_invocable: true
hidden: true
context: inline
---

## Newsletter Generation

Produce a complete, ready-to-publish newsletter in Markdown: curated items from multiple sources plus original analysis, in a consistent voice.

## When to Use

- "Create a newsletter about X", "write a weekly digest", "generate a tech roundup", "curate news about Y".
- A recurring format (daily digest, weekly roundup, monthly briefing) that needs the same structure each issue.
- A briefing that must be email-ready with links, sources, and attributions.

## When NOT to Use

- **Deep multi-source research with citations for a decision** — use the `deep-research` skill.
- **A research report, market analysis, or consulting memo** — use `market-research` or `consulting-analysis`.
- **Internal team updates or status reports** — use the `internal-comms` skill.
- **A single article, blog post, or tutorial** — write it directly; a newsletter is a curation container.

## Tools You Use

| Task | Tool | Key params |
|------|------|------------|
| Find current items | `web_search` | `query` |
| Read a source in full | `web_fetch` | `url`, `format` |
| Draft the issue | `file_write` | `path`, `content` |
| Send it | `email` | subject, to, body |
| Verify facts and dates | `grep` | `pattern`, `path` |

## Phase 1: Planning

### 1.1 Pin the parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| Topics | Subject area(s) to cover | Required |
| Format | Daily digest, weekly roundup, deep-dive, industry briefing | Weekly roundup |
| Audience | Technical, executive, general, niche | General |
| Tone | Professional, conversational, witty, analytical | Conversational-professional |
| Length | Short (5 min), medium (10 min), long (15 min+) | Medium |
| Sections | Number and type | 4-6 |
| Frequency | One-off or recurring series | One-off |

If the user did not say, use the defaults and state them once at the top of the draft so they can correct you.

### 1.2 Choose the structure

**Daily digest** — Top story (detailed), Quick hits (3-5 brief), One stat or quote, What to watch.

**Weekly roundup** — Editor's note, Top stories (2-3 detailed), Trends and analysis (1-2 with original commentary), Quick bites (4-6 brief), Tools and resources (2-3), Closing.

**Deep dive** — Introduction and context, Background, Key developments, Perspectives, What is next, Further reading.

**Industry briefing** — Executive summary, Market developments, Company moves, Risk and opportunity, What we think, Sources.

## Phase 2: Research and Curation

### 2.1 Gather candidates

Run several searches per topic — vary the phrasing, add date constraints, and search both English and the local language for regional beats. Aim for 2-3x the items you intend to publish, so curation is a real choice.

### 2.2 Verify before you include

Open each candidate with `web_fetch`. Confirm the headline, the date, and at least one specific fact or number. Anything you cannot verify is dropped, not hedged.

### 2.3 Rank

- **Recency and weight first:** what moved markets, launches, or decisions this period.
- **Diversity second:** do not run five items from one outlet or one angle.
- **Relevance third:** cut anything the audience can read elsewhere in one minute.

### 2.4 Write the commentary

The newsletter is only worth reading if it says something the sources do not. For the top items, add: what it means, who wins and loses, and what to watch next. Attribution stays explicit — quote or link the source, then write your view.

## Phase 3: Writing

### 2.1 Write each item in three parts

1. **Headline** — specific, not clever; name the thing that changed.
2. **Summary** — 2-4 sentences with the numbers and the date.
3. **So what** — one or two sentences of your read.

Brief items get one tight sentence plus a link.

### 2.2 Keep the voice constant

Decide the tone once and hold it. Witty works in one register and sounds forced in another; if the piece would sound different read aloud, cut it.

### 2.3 Format for scanning

- Headings per section, no more than four words.
- Bullet lists only for enumerable facts.
- Links after the sentence, never replacing it.
- One source per item, in parentheses or as a link label.

## Example

```
# Signal Weekly — Issue 42, Oct 24

**In brief:** Three model releases this week, one of them priced at less than a cent per million tokens. That is the trend, not the news.

## Top stories

**Volcengine ships Seedream 5.0 Pro with reasoning** — the release adds a reasoning step before generation and reports measurable gains on multi-image edits (source). It is the first of the Chinese labs to market image quality and generation reasoning as a single capability.

**Muse Spark 1.3 undercuts the frontier on price** — $1.25 per million input tokens against roughly $4.50 for the leader (source). The gap is now the story: capability is flattening while unit cost falls.

## Trends

**The price curve is the product.** Three weeks ago no model cleared 10M input tokens for under $2. Watch for API-native tools that priced themselves on frontier assumptions and now have to reprice.

## Quick bites

- Seedance 2.0 adds reference-image motion control (source).
- OpenAI's batch API pricing cut lands next week (source).

## What to watch

- Whether image-reasoning models land on the open weights side this month.

---
*Sources linked inline. Written Oct 24, 2026.*
```

## Quality Checklist

- Every item has a verified date and a real link.
- Top items carry original commentary, not paraphrase.
- Sources are spread across outlets and angles.
- Length matches the stated target.
- The voice is consistent from first line to last.
- Structure matches the issue number in a recurring series.

## Common Pitfalls

- **Headline without verification** — a wrong date in a newsletter is the fastest way to lose the list.
- **Rewriting a source instead of commenting on it** — then the reader should read the source.
- **Padding with filler items** to hit a section count; a shorter issue is stronger.
- **One outlet, three times** — looks like curation effort while it is one angle.
- **Chasing everything** — a newsletter that covers everything is trusted for nothing.
- **Breaking the format mid-series** — consistency is what makes a recurring digest valuable.
- **Unattributed numbers** — always name the source of a statistic.

## Best Practices

- Draft the editor's note last; it summarizes what the issues actually says.
- Keep a running archive file of past items so nothing repeats.
- Cap the number of sections by reading time, not by item count.
- Add a single "what to watch" line every issue; it is the highest-read part.
- For recurring series, pin the format in the first issue and reuse it unchanged.

## Anti-patterns

- A digest that is really a link dump with no editorial voice.
- Commentary that contradicts the source it cites.
- Publishing an item because it is fresh, not because it matters to this audience.

## Pairing with Other Tools

- `deep-research` — when a single item needs a full investigation.
- `market-research` / `consulting-analysis` — turning a series of issues into a report.
- `web_search` / `web_fetch` — the research layer.
- `email` — delivery.
- `writing-guidelines` — the final prose pass.
