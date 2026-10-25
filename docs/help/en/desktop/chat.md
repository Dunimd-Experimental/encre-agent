Conversation is the core of Encre Agent. This page shows how to organize sessions, manage messages, and read the agent's actions.

## Sessions

Each click on **New session** creates an independent session with its own context. Sessions support these operations:

| Operation | Description |
| --- | --- |
| Rename | Give the session a meaningful name for later reference |
| Export as Markdown | Export the whole session to a `.md` file |
| Archive | Hidden from the list, can be restored at any time |
| Delete | Permanently delete the session (bulk delete is also available) |
| Bulk actions | In "All tasks", select multiple sessions to export/delete at once |

### Session sidebar
The sidebar lists all sessions. You can:
- Click a session to switch to it
- Press `Ctrl+L` to start a new session
- Search session history at any time

![Session sidebar and action menu](/screenshots/session-sidebar.png)

## Temporary chat

Keep nothing? Use **Temporary chat** — it clears itself on exit, ideal for quick, low-stakes Q&A.

> [!NOTE]
> Temporary chat is not written to persistent history. Copy anything you need.

## Message actions

Hover over any message to reveal action buttons:

- **Edit**: modify the message and resend
- **Copy**: copy as plain text
- **Copy Markdown**: copy the Markdown source
- **Delete**: remove the message
- **Rollback edit**: undo this message and delete everything after it

### Regenerate
When a reply is interrupted or you want a different angle:
- **Regenerate**: regenerate with default settings
- **More detailed**: generate a longer reply
- **More concise**: generate a shorter reply

### Branching
On any assistant message you can create a **branch**. A branch forks a fresh continuation at that message without touching the original reply. Useful when you want to keep the current result while exploring another direction.

![Message actions and branch](/screenshots/message-branch.png)

## Input area

The input box at the bottom is the main entry point:

- **@ Attach files**: attach files/folders for the agent to read
- **Slash commands**: type `/` to bring up the command menu
- **Model picker**: switch the model for the current session
- **Thinking level**: when the model supports deep thinking you can adjust depth (default / deeper)
- **Send / Stop**: send (`Enter`), or stop mid-generation (`Shift+Enter` for a newline)

### Built-in commands

| Command | Purpose |
| --- | --- |
| `/plan` | Plan mode: makes a plan first, executes when ready |
| `/spec` | Spec mode: writes a spec before implementing |
| `/new-session` | New session |
| `/clear-session` | Clear the current session |
| `/init` | Initialize project: scan structure and generate `AGENTS.md` |

Custom commands see [Skills & Commands](/en/desktop/skills).

![Input area and command menu](/screenshots/input-tools.png)

## Reading the agent's actions: tool call cards

The agent completes tasks by calling tools. Each tool call shows up as a card with the tool name, arguments, and live status:

- Status: queued → running → success / failed
- Common tool cards: Bash/Shell/Terminal, Web Search, Web Fetch, Find Tool, Read File, Write File, Edit File, Apply Patch, MCP, LSP, Git, Memory, Task, Cron, Rest Client, Desktop, and more

Multiple cards running at once means the agent is doing **parallel tasks**; queued cards show "queued: {n} prompts waiting".

![A trail of tool call cards](/screenshots/tool-cards.png)

### Deep thinking (thought strip)
In sessions using a reasoning model, a collapsible "deep thinking" strip appears above messages. Expand it to review the agent's reasoning.

### Plan / spec components
In `/plan` or `/spec` mode, replies render as checkable task lists (plans) or structured specs.

## Errors and retries

When a request fails, an error card describes the type:

- **Rate limited**: too frequent or quota exhausted
- **Context overflow**: content exceeded the model's context window
- **Network error**, **server error**, **tool error**, **interrupted**

Mostly one retry suffices; for "context overflow", start a new session or ask the agent to summarize and retry with "More concise".

> [!TIP]
> Files created in a session are labeled **artifacts**; the session info panel shows how many the conversation produced.