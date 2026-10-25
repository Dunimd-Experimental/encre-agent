Here are the answers to the most common questions about using Encre Agent Desktop.

## General

**Q: How do I get started?**
A: Install the app → configure a model (Settings → Models) → ask at the input box. See [Quick Start](/en/desktop/getting-started).

**Q: The agent says no model is available?**
A: Add a model in Settings → Models (API key, model ID, base URL). Key setup: see [Models](/en/desktop/models).

**Q: The agent isn't replying / keeps spinning?**
A: Check network and model quota. Look at the error card on the reply: rate limited, context overflow, network error, server error, tool error, etc.

## Chat & sessions

**Q: Temporary chat vs regular session?**
A: A temporary chat clears itself on exit — good for quick Q&A; regular sessions persist.

**Q: I deleted a message by mistake — can I recover it?**
A: Use the "undo" shortcut right after a single delete. For whole sessions, export as Markdown first.

**Q: Reply too long or interrupted?**
A: Regenerate with "More concise", or hit Stop and continue asking.

**Q: Context overflow?**
A: Start a new session, or ask the agent to summarize conclusions first. Occasional "compaction" organizes context automatically. Trimming [Rules](/en/desktop/rules) also reduces context usage.

## Workspaces


**Q: Does removing a workspace delete files?**
A: No. It only unlinks the folder; disk files remain. Deleting sessions deletes session records.

## Automation

**Q: An automation task failed?**
A: Open "Execution history" for that task and return to chat to view the cause. Common codes: `-1` Bash timeout, `-2` Docker missing.

**Q: A task didn't run on time?**
A: Check the task isn't "paused", the schedule is correct, and the app was running then.

**Q: Where are results pushed to?**
A: Enable "push" on the task and select target gateways (e.g. Telegram, email). Setup: see [Gateway](/en/desktop/gateway).

## Permissions & safety

**Q: Why does the agent keep asking before running commands?**
A: The default permission mode asks before each risky operation for safety. If you trust the task, adjust policies or mode in [Permissions](/en/desktop/permissions).

**Q: How to reset permissions?**
A: Click "Reset to defaults" in the Permissions panel.

**Q: Is my data safe?**
A: Local data uses AES-256-GCM encryption; credentials are encrypted at rest. Additional defenses include SSRF protection, Docker sandbox, AI risk classification, and rate limiting.

## Models

**Q: Can I use several providers at once?**
A: Yes. Add models from multiple providers and switch at the input picker.

**Q: Does the model support images?**
A: Enable the "Multimodal" toggle for that model in settings (verified on save) and select it at the input.

## Other

**Q: How to contact support / report bugs?**
A: See [About & Updates](/en/desktop/about) for official docs, feedback, community, and contact.

**Q: How to update the app?**
A: Click "Check for updates" on the About page, or get the new version from your system's software sources.