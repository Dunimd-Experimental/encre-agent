---
name: writing-guidelines
description: "Audit docs and prose against the Writing Guidelines: voice, tone, sentence structure, terminology, capitalization, punctuation, formatting. Use for \"review my docs\", \"check writing style\", \"audit this page's tone\"."
---

# Writing Guidelines Review

Line-level review of documentation and prose against a versioned style
handbook, returning concrete findings instead of vague "could be clearer"
remarks.

## When to Use

- "Review these docs for style and tone."
- Before publishing a README, changelog, landing page or help article.
- Terminology drift across a doc set (product names, capitalization, "sign in" vs "log in").
- A translation or AI-drafted text needs a consistency pass in English.

## When NOT to Use

- Structural rewriting or ghost-writing a document — use `doc-coauthoring` or `write-docs`.
- Academic or citation-heavy writing — `academic-paper-review` / `systematic-literature-review`.
- Code comments and API reference generation — `code-documentation`.
- Copy strategy, naming and positioning — `internal-comms` or `consulting-analysis`.

## Workflow

1. **Fetch the current rules** (they change upstream):

   ```
   web_fetch(url="https://raw.githubusercontent.com/vercel-labs/writing-guidelines/main/command.md", format="markdown")
   ```

   Retry once via `terminal` (`curl -sL <url>`) if `web_fetch` fails. If both fail, report that the handbook is unavailable; do not fabricate rules.

2. **Identify the target files** from the argument (path or glob). If none were given, ask which files to review.

3. **Read the text** with `file_read`, or `grep` for specific patterns (banned words, passive constructions, inconsistent product names, heading capitalization).

4. **Check each rule** from the fetched handbook — sentence length and directness, active voice, second person, terminology, capitalization, headings, lists, links, numbers, punctuation, inclusive language.

5. **Report as `path:line — rule — problem — fix`**, grouped by file and ordered by severity. Keep it terse.

6. **Do not rewrite** unless asked; a review and a rewrite are different requests.

## Example

```
writing-guidelines docs/getting-started.md
```

```
docs/getting-started.md:12 — active-voice — "The config is loaded by the CLI" — "The CLI loads the config"
docs/getting-started.md:31 — terminology — "log in" here, "sign in" at line 44 — pick one term
```

## Best Practices

- Quote the offending phrase verbatim so the author can find it.
- Distinguish rule violations from taste; label the latter as optional.
- Flag systematic issues once (terminology, heading case) rather than repeating the same finding 40 times.
- Preserve the author's intent: a style fix must not change meaning.

## Common Pitfalls

- Rewriting the whole document when the request was a review.
- Applying rules to code blocks, front matter or quoted external text.
- Reporting a "passive voice" hit where the passive is correct (unknown actor, deliberate emphasis).
- Auditing against a remembered version of the handbook.

## Anti-patterns

- Vague feedback ("make it punchier") with no line reference.
- Imposing a house style the user never adopted.
- Changing product names or legal copy in the name of consistency.
