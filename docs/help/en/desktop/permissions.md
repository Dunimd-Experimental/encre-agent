Permissions determine how far the agent may "act". The defaults are already safe enough — loosen them only when you know what you're doing. Configure under **Settings → Permissions**.

## Permission policies

Every tool can have one of four policies:

| Policy | Behavior |
| --- | --- |
| Default | Follow the global default policy (recommended): risky operations ask first |
| Allow | Run automatically, no confirmation |
| Ask | Ask your approval before every execution |
| Deny | Completely forbidden; the agent cannot call it |

> [!NOTE]
> Under the default policy, dangerous tools like Bash ask for confirmation first; "Ask" applies to a single run and does not remember approvals.

## Manageable tools

The permissions panel lists essentially every tool, including:

- bash (terminal), ssh
- file_write / file_edit / apply_patch / archive
- docker, deploy, database
- browser, desktop, computer_use
- email, notify, env_manager, cloud_storage
- git, github, manage, swarm, workflow
- lint_format, rest_client, file_api, batch_api, fine_tuning_api
- agent, generate_image, edit_image, transcribe_audio, translate_audio
- create_embeddings, create_moderation
- task_create / task_stop, cron_create / cron_delete
- memory_create / update / delete, and more

![Permission policies](/screenshots/permissions-list.png)

## Preset permission modes

The app ships several golden permission modes that you can switch per session:

| Mode | Description |
| --- | --- |
| **Default** (recommended) | Asks before every operation — safest |
| Bypass | No confirmation at all — highest risk |
| Don't ask | Records and auto-allows |
| Accept edits | Auto-allows file-edit type operations |
| Plan | Makes a plan first, executes after confirmation |
| Auto | Lets the AI decide whether to ask |

> [!WARNING]
> "Bypass" makes the agent run anything directly. Use it only when you fully trust the task and the model.

## Extra protections

Beyond the permission system, Encre Agent layers more defenses:

- **SSRF protection**: DNS + CIDR blacklist blocks malicious requests
- **Docker sandbox**, Linux **Landlock** restrictions
- **AI risk classifier**: grades operation risk automatically
- **Rate limiting**: prevents abuse

## FAQ

**Q: I broke something — how to restore?**
A: The permissions panel has a "Reset to defaults" that returns to factory settings in one click.

**Q: A tool keeps asking and it's annoying?**
A: Set that tool's policy to "Allow" (only do this when you trust the context).

**Q: Why does it still ask sometimes even with Allow?**
A: Certain high-risk actions have built-in backstops: even with "Allow", the risk classifier may still trigger confirmation.