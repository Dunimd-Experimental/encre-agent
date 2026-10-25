---
name: defuddle
description: Extract clean markdown from web pages with the Defuddle CLI, stripping nav, ads and boilerplate. Use when a URL points to an article, docs page or blog post and you need its text. Not for .md URLs or logged-in pages.
---

# Defuddle

Defuddle turns a noisy HTML page into clean markdown: no nav bars, no cookie
banners, no sidebar links. It is the first thing to reach for when a URL is
content (article, docs, blog) rather than an application.

## When to Use

- The user pastes a URL and asks you to read, summarize, quote or analyze it.
- You need documentation or a blog post as context before writing code.
- A page is long and `web_fetch` returns megabytes of markup.
- You want a specific metadata field (`title`, `description`, `domain`) without the body.

## When NOT to Use

- The URL ends in `.md`, `.txt` or `.json` — it is already machine-readable, use `web_fetch`.
- The page requires a login, a session cookie or JavaScript-rendered content — use the `browser` tool instead.
- You need to click, scroll or fill a form — that is `browser`, not extraction.

## Prerequisites

The CLI must be on `PATH`:

```
npm install -g defuddle
```

Verify with `defuddle --version` through the `terminal` tool. If the install fails (no npm, restricted network), fall back to `web_fetch` with `format="markdown"`.

## Workflow

1. Confirm the URL is a normal HTML page (not `.md`/`.json`).
2. Run the CLI through `terminal`:

   ```
   defuddle parse <url> --md
   ```

3. If the output is long and you only need part of it, save it to disk and grep/read selectively:

   ```
   defuddle parse <url> --md -o content.md
   ```

4. Cite the URL in your answer; do not present extracted text as your own.

## Example

```
defuddle parse https://example.com/blog/post --md -o /tmp/post.md
defuddle parse https://example.com/blog/post -p title
```

## Output Formats

| Flag | Output | When |
|---|---|---|
| `--md` | Markdown | Default choice for reading and summarizing |
| `--json` | JSON with HTML and markdown | When you need structure as well as text |
| *(none)* | HTML | Rarely useful — larger and noisier |
| `-p <name>` | One metadata property | `title`, `description`, `domain`, `author` |

## Best Practices

- Always pass `--md`; without it you get HTML and lose the token savings.
- Write to a file when the page exceeds a screenful, then read the part you need — dumping a 40 KB markdown blob into the conversation wastes the budget you just saved.
- Extract metadata first (`-p title`) when you only need to identify a page.
- Keep the original URL in your summary so the user can verify the source.

## Common Pitfalls

- Running the command without checking `PATH` first, then reporting "tool not found" as a page failure.
- Piping the output through `head` and assuming the article ends there.
- Using Defuddle on a JS-only single-page app: the static HTML is often an empty shell.
- Forgetting that paywalled pages return a teaser, not the article.

## Anti-patterns

- Extracting a page you were not asked about, "just in case".
- Fetching the same URL repeatedly in one turn — cache the file and reuse it.
- Reproducing an entire copyrighted article when a summary was requested.
