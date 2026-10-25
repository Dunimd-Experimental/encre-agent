Workspace (iWork) mode lets Encre Agent work inside a local folder — the best way to write code, organize documents, and run project tasks. This page covers workspace setup and management.

> [!NOTE]
> The "Workspaces" area appears in the sidebar only after you switch to **Work** mode at the bottom.

## Open / create a workspace

1. Switch to **Work** mode
2. In the sidebar "Workspaces" area, click **New workspace** or **Open folder**
3. Choose any local folder

After opening, the app will:
- Register the folder as a workspace and show it in the sidebar
- Show how many sessions already exist in this workspace

![Create a workspace](/screenshots/workspace-create.png)


## Workspace settings

Click **Settings** on a workspace in the sidebar to:

- **Rename** the workspace (display name only; the disk path is untouched)
- **Upload an icon** to customize its appearance
- View the **path**, **ID**, and **session count**
- **Remove the workspace** (does not delete disk files — just unlinks it from the app)

## Archive management

Projects that are rarely used but important can be **archived**: sessions disappear from the list, and you can **unarchive** them anytime from the archive manager. Nothing is lost.

![Archive manager](/screenshots/workspace-archive.png)

## Working inside a workspace

Once a workspace is open, just describe the task in the input box. For example:

- "Read the README and summarize this project's tech stack"
- "Fix the unit tests that are failing in the tests directory"
- "Refactor `src/util.ts` and keep its interface unchanged"
- "Review the code quality and produce a report"

The agent will explore the project, read files, run commands, edit code, and cite `file:line` in its answers so you can verify.

![Agent citing files in the workspace](/screenshots/workspace-chat.png)

## FAQ

**Q: Nothing happens after I open a workspace?**
A: Check that a model is configured (see [Models](/en/desktop/models)).

**Q: Does removing a workspace delete files on disk?**
A: No. Removing only unlinks it from the app; disk files stay. Deleting workspace sessions deletes the sessions themselves.

**Q: Can one folder be registered as multiple workspaces?**
for clean session management