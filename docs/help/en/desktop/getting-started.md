Follow these steps and you will have Encre Agent Desktop up and running — and see it do real work in your first conversation — within about 5 minutes.

## Step 1: Install and launch

1. Download the installer for your system and install it:
   - **Windows**: `.exe` (NSIS installer)
   - **macOS**: `.pkg`
   - **Linux**: `.deb` / `.rpm`
2. Launch Encre Agent Desktop. You will see the main interface with a welcome screen.

![Main window after launch](/screenshots/launch-window.png)

## Step 2: Configure a model

Without a working model the agent cannot operate. Open **Settings → Models** from the bottom of the left sidebar:

1. Click **New** to add a model
2. Fill in the fields:
   - **Display name**: any name you will recognize, e.g. `DeepSeek`
   - **Model ID**: the model identifier, e.g. `deepseek-chat`
   - **Provider**: pick or type your model provider
   - **API key**: paste your key (created at the provider's console)
   - **Base URL**: endpoint; API paths are usually appended automatically
   - **Max tokens**: maximum output length per reply
3. If the model supports images and other multimodal input, switch on **Multimodal** (it is verified on save)
4. Click **Save**

> [!TIP]
> Models differ in "reasoning / thinking level" support. Enable Thinking Level for models that support it in the model settings so you can choose it next to the input box. The settings page also offers "Get API key" and "View docs" links.

## Step 3: Start your first conversation

1. Type a sentence at the bottom input box, for example:
   - "Write a Python script that merges all .txt files in the current directory into one file"
2. Click the **Send** button (or press `Enter`)
3. Watch the **tool-call cards** on the right: the agent will run tools like `Bash`, `Write File` and show you the results
4. The reply shows real paths and command output so you can verify its work

![First conversation with tool calls](/screenshots/first-chat-toolcalls.png)

## Step 4: Try something more complex

In **Workspace** mode, open a folder and tell the agent what you need (for example "scan this project's code quality and produce a report"). It will:

- Explore the project structure on its own
- Read relevant files
- Run commands to verify
- Answer with file references

See the [Workspace](/en/desktop/workspace) page for details.

## Next steps

- Understand the three flow modes: [Three Modes](/en/desktop/modes)
- Go deeper into sessions and branches: [Chat & Sessions](/en/desktop/chat)
- Let the agent work on a schedule: [Automation](/en/desktop/automation)

> [!WARNING]
> The agent really does modify files and run commands. In important directories, keep the **Default** permission mode (see [Permissions](/en/desktop/permissions)) so every step asks for your approval first.