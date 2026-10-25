Skills are capability packs the agent can use on demand; commands are your own shortcut prompts. Both are managed in **Settings → Skills & Commands**.

## Skills

Skills are installed as Markdown (`SKILL.md`) or zip packages — plug-and-play instructions/knowledge for the agent. Encre Agent ships with many built-in skills: debug, loop, batch, verify, stuck-recovery, code-review, refactor, gen-test, web-research, data-viz, write-docs, covering debugging, batch processing, code review, refactoring, test generation, web research, data visualization, and documentation.

### Enable / disable

Toggle the switch in the skill list to enable. Enabled skills are called by the agent when appropriate.

> More skills are installable from the online skill marketplace (see the community links on the About page).

## Custom slash commands

Commands turn your favorite prompts into quick shortcuts. Type `/` at the input box to open the command menu.

### Creating a command

In **Skills & Commands**, click **New command**:

| Field | Description |
| --- | --- |
| Name | What follows the slash; e.g. `review` → type `/review` |
| Title | The name shown in the command menu |
| Description | What this command does |
| Icon | A lucide icon name, e.g. `file-code` |
| Prompt | What is actually sent to the agent (can include placeholders) |

> [!NOTE]
> Command names may only contain **letters, numbers, and hyphens**.

Custom commands work well for shared team standards, e.g. a weekly-report template or a code-review phrase everyone uses.

![Skills and commands management](/screenshots/skills-list.png)

## Built-in commands recap

Typing `/` brings up: `/plan`, `/spec`, `/new-session`, `/clear-session`, `/init` and more (see [Chat & Sessions](/en/desktop/chat)).

## FAQ

**Q: What's the difference between skills and commands?**
A: Skills extend the agent's abilities/knowledge (called automatically or on demand); commands are input shortcuts for you to quickly start a fixed-format request.

**Q: Do skills affect safety?**
A: Skills are just prompt capability packs; actual execution still obeys [Permissions](/en/desktop/permissions).