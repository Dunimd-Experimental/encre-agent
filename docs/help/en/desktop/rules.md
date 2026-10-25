Rules are constraints the agent must obey. Under **Settings → Rules** there are two kinds: **global rules** and **project rules**.

## Global rules

Apply to all sessions, stored in `~/.dunimd/encre/rules/`. Good for generic behavior constraints, for example:

- Give the conclusion first, then elaborate
- State your plan before sensitive operations
- Always reply in English

## Project rules

Only apply to the current workspace, for example:

- `.encre/rules.md`: the project rule Encre Agent reads first
- Also auto-detects ecosystem conventions like `.cursorrules`, `.clinerules`, etc.

Good for project-specific conventions:

- Code style (space indentation, single quotes, ESM)
- "Never modify the `dist/` directory"
- "Use Conventional Commits for commit messages"

## Editing rules

- Open the rules list and click a rule to edit
- Saves take effect immediately (no restart needed)

> [!NOTE]
> Global and project rules are **mandatory constraints**, in effect together. When they conflict, project rules usually win in prompts, depending on session context.

## How rules work

Rules are injected into the agent's context every session. The number of active rules affects context usage, so keep them lean and focused.

![Rules management](/screenshots/rules-list.png)

## FAQ

**Q: Rules don't take effect?**
A: Check they are saved, file paths are correct; for project rules confirm they match the current workspace.

**Q: Can I disable a global rule temporarily for a project?**
A: Rules are bound globally. For finer control, say "ignore rule X for this session" in chat. For real isolation use a separate workspace and write exceptions in its project rules.