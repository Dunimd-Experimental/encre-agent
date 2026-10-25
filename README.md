<div align="center">

<img src="desktop/renderer/assets/EA-lm.svg" alt="Encre Agent" width="160"/>

English | [简体中文](README.zh.md)

[Documentation](https://dunimd.github.io/encre) | [Changelog](docs/CHANGELOG.md) | [Security](docs/SECURITY.md) | [Contributing](docs/CONTRIBUTING.md) | [Code of Conduct](docs/CODE_OF_CONDUCT.md)

<a href="https://github.com/Dunimd/encre-agent" target="_blank">
    <img alt="GitHub" src="https://img.shields.io/badge/GitHub-encre-181717?style=flat-square&logo=github"/>
</a>
<a href="https://gitee.com/dunimd/encre-agent" target="_blank">
    <img alt="Gitee" src="https://img.shields.io/badge/Gitee-encre-C71D23?style=flat-square&logo=gitee"/>
</a>

<a href="https://x.com/Dunimd2025" target="_blank">
    <img alt="X" src="https://img.shields.io/badge/X-Dunimd-000000?style=flat-square&logo=x"/>
</a>
<a href="https://space.bilibili.com/3493284091529457" target="_blank">
    <img alt="BiliBili" src="https://img.shields.io/badge/BiliBili-Dunimd-00A1D6?style=flat-square&logo=bilibili"/>
</a>
<a href="https://huggingface.co/dunimd" target="_blank">
    <img alt="Hugging Face" src="https://img.shields.io/badge/Hugging%20Face-Dunimd-FFD21E?style=flat-square&logo=huggingface"/>
</a>
<a href="https://modelscope.cn/organization/dunimd" target="_blank">
    <img alt="ModelScope" src="https://img.shields.io/badge/ModelScope-Dunimd-1E6CFF?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMTQiIGhlaWdodD0iMTQiIHZpZXdCb3g9IjAgMCAxNCAxNCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTcuMDA2IDBDMy4xNDIgMCAwIDMuMTQyIDAgNy4wMDZTMy4xNDIgMTQuMDEyIDcuMDA2IDE0LjAxMkMxMC44NyAxNC4wMTIgMTQuMDEyIDEwLjg3IDE0LjAxMiA3LjAwNkMxNC4wMTIgMy4xNDIgMTAuODcgMCA3LjAwNiAwWiIgZmlsbD0iIzFFNkNGRiIvPgo8L3N2Zz4K"/>
</a>

<a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank">
    <img alt="License" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg?style=flat-square"/>
</a>

**Encre Agent** — A powerful AI Agent platform that supports 51 mainstream LLM providers, 74 built-in tools, 24 chat platform integrations, and a plugin market with 256 official packages. Whether it's coding, desktop automation, cross-platform messaging, or multi-agent collaboration, Encre Agent gets the job done.

</div>

<h2 align="center">🤖 What is Encre Agent?</h2>

### One-Liner

Encre Agent is an AI Agent platform — tell it what you want to do, and it will automatically analyze, use tools, execute tasks, and deliver results to you.

### Core Capabilities

<div align="center">

| Capability | Description |
|:------|:------|
| 🧠 **51 AI Models** | Supports OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Qwen, GLM, local models, and more — switch freely |
| 🛠️ **74 Built-in Tools** | File operations, shell execution, browser automation (CDP engine), code editing, web search, databases, scheduling, Docker, Git, image gen, email — ready to use |
| 💬 **24 Chat Platforms** | Integrate with Telegram, Discord, Slack, DingTalk, WeChat, WhatsApp, email, QQ Bot, Matrix, and more — chat with Agent wherever you are |
| 🖥️ **Desktop Automation** | Control desktop apps, operate browsers, read screenshots — simulates human operations |
| 🤝 **Multi-Agent Collaboration** | Multiple Agents work simultaneously, dividing tasks and cooperating on complex projects |
| 💾 **Persistent Memory** | Agent remembers your preferences, habits, and project info — gets smarter with use |
| 🛡️ **Safety & Control** | 7 permission modes to precisely control what Agent can and cannot do |

</div>

### Use Cases

- **Code Development Assistant** — You find a bug → tell Encre Agent → it reads the code, runs LSP diagnostics, applies a patch, runs tests, and reports the fix
- **Cross-Platform Research** — Give Encre Agent one prompt like "research the best AI coding tools for me" → it opens a browser, searches Google, clicks articles, reads content, and hands you a comparison table
- **Desktop Automation** — Batch file processing, auto-form filling, screenshot archiving
- **Cross-Platform Chat Bot** — Connect Encre Agent to WeChat, Telegram, Slack, or DingTalk all at once, talk to it from any platform
- **Data Analysis** — "Read this PDF/Excel, summarize it, and tell me the key numbers"
- **Long-term Task Scheduling** — "Check my server every hour and alert me on WeChat if CPU > 80%"
- **Multi-Agent Collaboration** — Tell an architect Agent to plan a feature, a coder Agent to implement, and a critic Agent to review — they share a blackboard and work together

<h2 align="center">⭐ Key Features</h2>

#### 🧠 51 AI Backends, Choose Freely

Supports **51 mainstream AI providers** including OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Alibaba Qwen, Tencent Hunyuan, Xiaomi MiMo, Moonshot Kimi, Zhipu GLM, MiniMax, and more.

- **Global Leaders**: OpenAI, Anthropic, Google, Groq, AWS Bedrock, GitHub Copilot
- **Chinese Providers**: Alibaba Qwen, Tencent, Xiaomi MiMo, Moonshot, Zhipu, MiniMax
- **Self-hosted**: Ollama, LM Studio, HuggingFace Transformers
- **Aggregation Services**: OpenRouter, AI Gateway, Kilocode, OpenCode
- **Advanced Features**: Automatic failover, cost-based routing, transparent retry

#### 🤖 6 Built-in Agent Roles

Encre Agent comes with specialized roles, each with dedicated prompts and capabilities:

- **General Mode**: `coder`, `researcher`, `critic`
- **Workspace Mode**: `architect`, `planner`
- **Plan/Spec Mode**: `spec-writer`

#### 🛠️ 74 Ready-to-use Tools

| Category | Tools |
|:------|:------|
| **Files** | Read, write, edit, patch, search, PDF/Excel processing |
| **Terminal** | Shell commands, background tasks, Docker, deployment |
| **Web** | Page fetching, web search (MCP-powered), browser automation (CDP — Chrome DevTools Protocol) |
| **Dev Aid** | LSP diagnostics, IPython notebook, database queries |
| **Tasks** | Create/get/list/update/stop tasks with bash/agent/workflow executors |
| **Schedule & Memory** | Cron jobs, todo list, memory management |
| **Desktop** | Desktop control (pyautogui), image recognition (OCR), browser automation (CDP) |
| **Integration** | Git, REST API, MCP external protocols |

#### 💬 24 Chat Platform Integrations

Connect Encre Agent to your favorite messaging platforms:

**International**: Discord, Slack, Telegram, WhatsApp, Signal, Matrix, Microsoft Teams, Google Chat, Mattermost, Line, IRC, ntfy, SimpleX, Photon, Raft, Home Assistant

**China**: DingTalk, WeCom, WeChat, QQ Bot

**Universal**: Email (IMAP+SMTP), SMS, Webhook, iMessage (BlueBubbles)

Each platform has a dedicated adapter that normalizes messages before sending them to the Agent.

#### 🛡️ 7-Level Safety Control

| Mode | Description |
|:------|:------|
| `bypass` | No checks (fully open) |
| `dont_ask` | Auto-allow (no confirmation needed) |
| `accept_edits` | Auto-allow file edits |
| `plan` | Plan before executing |
| `spec` | Write a structured spec before implementing |
| `auto` | AI smart judgment |
| `default` | **Recommended** — asks you every time |

Additional protections:
- **SSRF Guard**: DNS resolution + CIDR blocklist
- **Sandbox Isolation**: Docker container sandbox + Linux Landlock kernel-level restrictions
- **AI Safety Classifier**: Agent autonomously assesses operation risk level
- **Rate Limiting**: Prevents runaway API calls

#### 💾 Persistent Agent Memory

Encre Agent has its own memory system — it gets to know you better over time:

- Memories stored as a weighted knowledge graph — episodes, facts, preferences, decisions, and lessons, each with a confidence score
- **Confidence Decay & Reinforcement**: unused memories fade over time; task outcomes reinforce or correct existing beliefs
- **Semantic Search**: Quickly find relevant memories through semantic understanding
- **Entity Hubs & Cross-links**: Related memories are wired by co-occurrence and semantic similarity for one-hop recall

#### 🎯 9 Professional Skills

Agent has 9 callable skills:

`debug`, `loop`, `stuck`, `code-review`, `refactor`, `gen-test`, `verify`, `write-docs`, `chart-visualization`

**Priority Override**: Managed > User > Project > Bundled — meets your personalization needs.

#### 🤝 Multi-Agent Collaboration

- **Swarm System**: Multiple Agents work concurrently without interference
- **Shared Blackboard**: All Agents share state with full transparency
- **Consensus Protocol**: Multiple Agents vote on proposals to select the best solution
- **Task Planner**: Automatically decomposes complex tasks into sub-task trees
- **6 Built-in Roles**: Each role has dedicated prompts and capabilities

#### 🧱 Encre Agent Core (Rust Native)

Encre Agent Core — performance-critical modules written in Rust for blazing speed and memory safety:

- Blazing fast file I/O with offset/limit support
- Regex search & Glob for quick content finding
- SIMD-accelerated text matching (hardware-level)
- Unified Diff for precise code difference calculation
- Sandboxed shell execution in secure environments
- Kernel-level filesystem restrictions via Linux Landlock
- Smart Token counting (English/Chinese/numbers/code)
- Semantic similarity for text comparison
- LSP protocol parsing (JSON-RPC 2.0)

#### 🖥️ Encre Agent Desktop

A full-featured AI chat desktop application built with Electron + React 19:

- **Chat Interface**: Markdown rendering, syntax highlighting, file attachments
- **Multi-session**: Concurrent chats with branching and fuzzy search
- **Settings Panel**: Model config, gateway, agent, MCP servers, skills, rules, memory
- **iClaw Mode**: Automation runner with batch operations
- **Embedded Terminal**: xterm.js + node-pty with working directory browser
- **Code Editor**: Monaco Editor with 16+ language support (TypeScript, JavaScript, Python, Rust, Go, Java, C/C++, PHP, Ruby, Swift, Kotlin, SQL, etc.)
- **11 UI Languages**: English, Simplified/Traditional Chinese, Japanese, Korean, German, Spanish, Portuguese, Turkish, Arabic, Hebrew — with runtime switching
- **Git Integration**: Status and diff with caching
- **System Tray**: Notifications + voice input support
- **Encrypted Storage**: AES-256-GCM encrypted browser cookies
- **Cross-platform**: Windows (NSIS), macOS (.pkg), Linux (.deb/.rpm)

#### 🧩 Plugin Market & Central Registry

An open plugin ecosystem backed by a PyPI-based central registry — install skills and tools directly from the built-in market:

- **One Wheel, Any Python**: Every market package is a universal pure-Python wheel supporting **Python 3.11+** — build once, install anywhere
- **ea-plugin-cli**: The unified toolchain that scaffolds, builds, verifies, and publishes plugins — [ea-plugin-cli](https://github.com/mf2023/ea-plugin-cli)
- **ea-cwh Central Registry**: The authoritative plugin catalog shipped as a PyPI data package; the market UI reads it fully offline — [ea-cwh](https://github.com/mf2023/ea-cwh)
- **256 Official Packages**: `ea-skill-*` / `ea-tool-*` / `ea-connector-*` packages covering data, media, web, dev, travel, finance, and messaging platforms
- **Verified Lifecycle**: sha256 digest verification, sandboxed install verification, and CI-gated catalog merges — a plugin can only reach the market through validated pipelines

#### ⚙️ Developer Toolchain

- **Context Compaction**: 9 strategies to intelligently manage conversation context
- **LSP Client**: 16+ language servers auto-discovered for real-time code diagnostics
- **Evolution System**: Meta-cognition, reflex loop, strategy optimizer
- **Feedback Learner**: Jaccard similarity-based error correction
- **Interactive Notebook**: IPython kernel for live code execution
- **Plugin System**: Extensible architecture with plugin registry
- **User Profile**: Persona inference and soul system
- **Spec Engine**: Structured specification document management
- **WebSocket Server**: Real-time communication + admin HTTP API

<h2 align="center">🚀 Quick Start</h2>

### Get Started in 3 Steps

**Step 1: Clone the repository**

```bash
git clone https://github.com/Dunimd/encre-agent.git
cd encre-agent
```

**Step 2: Build everything**

```bash
python build.py
```

This builds the Rust extension, installs the Python package, and bundles the Encre Agent Desktop — all in one command.

**Step 3: Launch**

```bash
# Encre Agent Desktop
cd desktop && npm start

# Or use the CLI (iClaw mode)
python -m encre.iclaw --help
```

### Basic Usage

Start chatting with your Agent through the Encre Agent Desktop, or connect it to your preferred chat platform. Configure your AI model provider, set the permission mode, and you're ready to go.

### Configuration

Configuration is stored as encrypted JSON files under the data directory (`~/.dunimd/encre/config/`) and is normally managed from the Encre Agent Desktop settings panel:

```json
{
  "providers": [
    {
      "backend_type": "openai",
      "name": "OpenAI",
      "api_key": "<your-api-key>",
      "models": [
        { "name": "GPT-6 Astra", "model_id": "gpt-6-astra", "enabled": true },
        { "name": "GPT-6 Luna", "model_id": "gpt-6-luna", "enabled": true },
        { "name": "GPT-6 Sol", "model_id": "gpt-6-sol", "enabled": true }
      ]
    }
  ]
}
```

Config files under `<data_dir>/config/`:
1. `models.json` — model endpoints, active model, failover
2. `agents.json` — agent personas, sub-agents, system prompt, skills
3. `adapters.json` — platform adapter credentials
4. `mcp.json` — MCP server definitions
5. `ui.json` — presentation preferences
6. `runtime.json` — behaviour flags, permissions, and other runtime fields

Environment variables use the `ENCRE_` prefix (e.g. `ENCRE_DATA_DIR`, `ENCRE_PROFILE`) and override data-directory and profile selection.

<h2 align="center">❓ Frequently Asked Questions</h2>

**Q: Which AI models does Encre Agent support?**

A: **51 providers** including OpenAI, Anthropic Claude, Google Gemini, DeepSeek, Alibaba Qwen, Tencent Hunyuan, Xiaomi MiMo, Moonshot Kimi, Zhipu GLM, MiniMax, Ollama, LM Studio, HuggingFace, AWS Bedrock, OpenRouter, and more. Encre Agent is built from scratch — not a fork of Codex or any other project.

**Q: What can Encre Agent do?**

A: Everything a skilled assistant can do — read/write/edit code, run shell commands, open a browser to do research, automate your desktop, manage files, schedule tasks, collaborate with other Agents, and much more. It has **74 built-in tools** and **185 skill packages** to handle almost any task.

**Q: Can I use Encre Agent with my favorite chat platform?**

A: Yes! Encre Agent supports **24 chat platforms** including Telegram, Discord, Slack, DingTalk, WeCom, WeChat, WhatsApp, QQ Bot, Email, Matrix, and more. Each platform has a dedicated adapter.

**Q: Is Encre Agent safe to use?**

A: Safety is a top priority. Encre Agent offers **7 permission modes** ranging from fully open to asking for confirmation on every action. Additional protections include SSRF guards, Docker sandboxing, Landlock kernel restrictions, and AI-powered risk classification.

**Q: Does Encre Agent remember things between sessions?**

A: Yes. Encre Agent has a **persistent memory system** that stores important information, learns your preferences, and maintains context across conversations. Over time, it gets smarter and more personalized.

**Q: Can multiple Agents work together?**

A: Absolutely. Encre Agent supports **multi-agent collaboration** through its Swarm system. You can spawn multiple Agents with different roles (coder, researcher, critic, etc.) that share information and cooperate on complex tasks.

**Q: Is there an Encre Agent Desktop?**

A: Yes! **Encre Agent Desktop** is a full-featured Electron + React application with chat interface, code editor (Monaco), embedded terminal, file browser, settings panel, and more. Available for Windows, macOS, and Linux.

**Q: Can I use my own AI models?**

A: Yes. You can use any OpenAI-compatible API, self-host models via Ollama/LM Studio, or integrate custom backends. Encre Agent is designed to be backend-agnostic.

<h2 align="center">🌏 Community & License</h2>

- Issues and PRs are welcome!
- GitHub: https://github.com/Dunimd/encre-agent
- Gitee: https://gitee.com/dunimd/encre-agent
- GitCode: https://gitcode.com/dunimd/encre-agent

<div align="center">

## 📄 License & Open Source Agreements

### Project License

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="Apache License 2.0">
  </a>
</p>

This project uses **Apache License 2.0**. See [LICENSE](LICENSE) for the full text.

### Dependency Licenses

The dependency license list below reflects the packages used by the **Python gateway**, **Encre Agent Core** (Rust native engine), and **Encre Agent Desktop** (Electron app). Versions and exact license texts are not pinned here; consult each upstream project for the authoritative license.

<div align="center">

| Package | License | Package | License |
|:-----------|:-----------|:-----------|:-----------|
| httpx | BSD-3-Clause | pydantic | MIT |
| beautifulsoup4 | MIT | markdownify | MIT |
| lxml | BSD-3-Clause | tomli | MIT |
| tomli-w | MIT | pyyaml | MIT |
| cryptography | Apache-2.0 / BSD | zero-api-key-web-search | Apache-2.0 |
| pathspec | MPL-2.0 | websockets | BSD-3-Clause |
| Pillow | Historical | (built-in) | CDP-based browser engine |
| tiktoken | MIT | numpy | BSD-3-Clause |
| loguru | MIT | openai | Apache-2.0 |
| anthropic | MIT | google-generativeai | Apache-2.0 |
| ollama | MIT | groq | MIT |
| aiohttp | Apache-2.0 | discord.py | MIT |
| slack_bolt | MIT | slack_sdk | MIT |
| python-telegram-bot | GPL-3.0 | dingtalk-stream | MIT |
| aioimaplib | BSD-3-Clause | aiosmtplib | MIT |
| torch | BSD-3-Clause | transformers | Apache-2.0 |
| boto3 | Apache-2.0 | pytest | MIT |
| pytest-asyncio | Apache-2.0 | ruff | MIT |
| mypy | MIT | pre-commit | MIT |
| mss | MIT | openpyxl | MIT |
| pdfplumber | MIT | pyautogui | BSD-3-Clause |
| pypdf | BSD-3-Clause | PyPDF2 | BSD-3-Clause |
| pytesseract | Apache-2.0 | uiautomation | MIT |
| watchfiles | MIT | | |
| serde | MIT/Apache-2.0 | serde_json | MIT/Apache-2.0 |
| regex | MIT/Apache-2.0 | walkdir | MIT/Apache-2.0 |
| similar | MIT | glob | MIT/Apache-2.0 |
| tempfile | MIT/Apache-2.0 | candle-core | Apache-2.0 |
| tokenizers | Apache-2.0 | wide | MIT/Apache-2.0 |
| pyo3 | MIT/Apache-2.0 | | |
| electron | MIT | electron-builder | MIT |
| esbuild | MIT | typescript | Apache-2.0 |
| @xterm/xterm | MIT | @xterm/addon-fit | MIT |
| @xterm/addon-webgl | MIT | node-pty | MIT |
| markdown-it | MIT | highlight.js | BSD-3-Clause |
| minisearch | MIT | monaco-editor | MIT |
| react | MIT | react-dom | MIT |
| simple-icons | CC0-1.0 | | |

</div>

</div>