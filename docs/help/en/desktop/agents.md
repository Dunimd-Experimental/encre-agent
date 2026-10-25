Agents (sub-agents) are specialized assistants that run with their own system prompt. Giving fixed responsibilities to dedicated agents lets the main agent work with clearer roles.

## Creating an agent

In **Settings → Agents**, click **New** and configure:

| Field | Description |
| --- | --- |
| Name | Used to reference the agent in chat (e.g. `code-reviewer`) |
| System prompt | Defines this agent's role and behavior |

For example, create a `code-reviewer`:
- Name: `code-reviewer`
- Prompt: "You are a senior code reviewer focused on security, maintainability, and latent defects; give suggestions graded by severity."

After saving, the main agent may call on suitable agents when handling related tasks.

![Agent management](/screenshots/agents-list.png)

## Usage

- Describe the task in chat and mention which agent should join, e.g. "Ask code-reviewer to review this code"
- Or create small assistants for specific roles and use them by role

## Built-in agent roles

Encre Agent ships with predefined roles that cooperate automatically:

- **General**: coder, researcher, critic
- **Workspace**: architect, planner
- **Plan/spec**: spec-writer
- **Automation**: monitor, executor, scheduler

## Agent config in session info

The session info panel shows the current agent configuration (specialty, permission mode, max turns, send mode), so you can see which role this session is running as.

> [!TIP]
> Every agent is governed by global [Permissions](/en/desktop/permissions) — no matter how clear a role is, dangerous operations still ask for your approval first.