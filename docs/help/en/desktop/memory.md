Memory lets Encre Agent keep important information across sessions — the more you use it, the better it understands you. View and manage it under **Settings → Memory**.

## What memory is

Persistent memory is stored as Markdown files with metadata; each entry includes:

- Name / title
- Content preview
- Extra info (created, updated, importance, etc.)

The agent **actively writes** what it finds worth remembering (your preferences, project background), and references it later when needed.

## Viewing and refreshing

- Open **Settings → Memory** to browse all entries
- Click an entry to see details
- Hit **Refresh** when you want the latest state

## Two layers of memory

Encre Agent distinguishes **working memory** (current session/task related) from **long-term memory** (accumulated across sessions). Combined with **smart aging**, old low-importance entries fade automatically to stop context bloat.

## Smart properties

- **Semantic search**: the agent retrieves by meaning, not just keywords
- **Automatic aging**: low-importance, unused entries gradually deprioritize
- **Proactive capture**: valuable info is summarized and kept after tasks

![Memory management](/screenshots/memory-list.png)

## FAQ

**Q: Is memory visible to other sessions?**
A: Yes — it's shared so it can serve you across sessions. If something must not enter long-term memory, say "don't remember this" explicitly in chat.

**Q: Does memory take up space?**
A: Entries are tiny; the app also tidies and ages regularly. Space details are in [Storage](/en/desktop/storage).

**Q: Can I delete one entry manually?**
A: Yes, delete it in the memory list.

> [!TIP]
> To make the agent remember something strongly, just say so in chat: e.g. "Please remember: I prefer to write projects in TypeScript."