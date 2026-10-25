<div align="center">

<img src="desktop/renderer/assets/EA-lm.svg" alt="Encre Agent" width="160"/>

[English](README.md) | 简体中文

[文档](https://dunimd.github.io/encre) | [更新日志](docs/CHANGELOG.md) | [安全](docs/SECURITY.md) | [贡献指南](docs/CONTRIBUTING.md) | [行为准则](docs/CODE_OF_CONDUCT.md)

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

**Encre Agent** — 强大的 AI Agent 平台，支持 51 个主流 LLM 提供商、74 个内置工具、24 个聊天平台集成，以及拥有 256 个官方插件的插件市场。无论是编程开发、桌面自动化、跨平台消息，还是多智能体协作，Encre Agent 都能胜任。

</div>

<h2 align="center">🤖 什么是 Encre Agent？</h2>

### 一句话简介

Encre Agent 是一个 AI Agent 平台 —— 告诉它你想做什么，它会自动分析、调用工具、执行任务并将结果交付给你。

### 核心能力

<div align="center">

| 能力 | 说明 |
|:------|:------|
| 🧠 **51 个 AI 模型** | 支持 OpenAI、Anthropic Claude、Google Gemini、DeepSeek、Qwen、GLM 以及本地模型等，自由切换 |
| 🛠️ **74 个内置工具** | 文件操作、Shell 执行、浏览器自动化（CDP 引擎）、代码编辑、网页搜索、数据库、调度、Docker、Git、图片生成、邮件，开箱即用 |
| 💬 **24 个聊天平台** | 接入 Telegram、Discord、Slack、钉钉、微信、WhatsApp、QQ Bot、Matrix、邮件，随时随地与 Agent 对话 |
| 🖥️ **桌面自动化** | 控制桌面应用、操作浏览器、读取屏幕截图，模拟人工操作 |
| 🤝 **多智能体协作** | 多个 Agent 同时运行，分工协作完成复杂项目 |
| 💾 **持久化记忆** | Agent 会记住你的偏好、习惯与项目信息，越用越聪明 |
| 🛡️ **安全与控制** | 7 种权限模式，精确控制 Agent 的能力边界 |

</div>

### 应用场景

- **代码开发助手** — 拿到一段代码，发现一个 bug → 告诉 Encre Agent → 它自动读代码、跑 LSP 诊断、打补丁、跑测试，告诉你怎么修好的
- **浏览器调研** — 给它一句提示词 "调研一下现在最好的 AI 编程工具" → 它自己打开浏览器、搜 Google、翻结果、点链接、读文章、最后给你一张对比表
- **桌面自动化** — 批量文件处理、自动填表、截图归档
- **跨平台聊天机器人** — 同时接入微信、Telegram、钉钉、Slack，从一个平台跟 Agent 对话
- **数据分析** — "读一下这份 PDF / Excel，帮我总结关键数据"
- **长期任务调度** — "每小时检查一次服务器，CPU 超过 80% 就在微信上通知我"
- **多智能体协作** — 架构师 Agent 规划、程序员 Agent 写代码、评审 Agent 审查，共享黑板一起干活

<h2 align="center">⭐ 核心特性</h2>

#### 🧠 51 个 AI 后端，自由切换

支持 **51 个主流 AI 提供商**，包括 OpenAI、Anthropic Claude、Google Gemini、DeepSeek、阿里 Qwen、腾讯混元、小米 MiMo、月之暗面 Kimi、智谱 GLM、MiniMax 等。

- **全球领先**：OpenAI、Anthropic、Google、Groq、AWS Bedrock、GitHub Copilot
- **中文与区域**：阿里 Qwen、腾讯、小米 MiMo、月之暗面、智谱、MiniMax
- **自托管**：Ollama、LM Studio、HuggingFace Transformers
- **聚合服务**：OpenRouter、AI Gateway、Kilocode、OpenCode
- **高级特性**：自动故障转移、按成本路由、透明重试

#### 🤖 6 个内置 Agent 角色

Encre Agent 内置了专用角色，每个角色都配有专属的提示词与能力：

- **通用模式**：`coder`、`researcher`、`critic`
- **工作区模式**：`architect`、`planner`
- **计划 / 规范模式**：`spec-writer`

#### 🛠️ 74 个开箱即用的工具

| 类别 | 工具 |
|:------|:------|
| **文件** | 读取、写入、编辑、补丁、搜索、PDF / Excel 处理 |
| **终端** | Shell 命令、后台任务、Docker、部署 |
| **网络** | 网页抓取、网页搜索（基于 MCP）、浏览器自动化（CDP） |
| **开发辅助** | LSP 诊断、IPython Notebook、数据库查询 |
| **任务** | 创建 / 获取 / 列出 / 更新 / 停止任务，含 bash / agent / workflow 执行器 |
| **调度与记忆** | 定时任务、待办清单、记忆管理 |
| **桌面** | 桌面控制（pyautogui）、图像识别（OCR）、浏览器自动化 |
| **集成** | Git、REST API、MCP 外部协议 |

#### 💬 24 个聊天平台集成

将 Encre Agent 接入你常用的消息平台：

**国际**：Discord、Slack、Telegram、WhatsApp、Signal、Matrix、Microsoft Teams、Google Chat、Mattermost、Line、IRC、ntfy、SimpleX、Photon、Raft、Home Assistant

**中国**：钉钉、企业微信、微信、QQ Bot

**通用**：邮件（IMAP + SMTP）、短信、Webhook、iMessage（BlueBubbles）

每个平台都有专属的适配器，在消息送达 Agent 前进行归一化处理。

#### 🛡️ 7 级安全控制

| 模式 | 说明 |
|:------|:------|
| `bypass` | 无检查（完全开放） |
| `dont_ask` | 自动允许（无需确认） |
| `accept_edits` | 自动允许文件编辑 |
| `plan` | 先规划再执行 |
| `spec` | 先写规范文档再实现 |
| `auto` | AI 智能判断 |
| `default` | **推荐** —— 每次操作前询问 |

额外保护：

- **SSRF 防护**：DNS 解析 + CIDR 黑名单
- **沙箱隔离**：Docker 容器沙箱 + Linux Landlock 内核级限制
- **AI 安全分类器**：Agent 自主评估操作风险等级
- **限流**：防止 API 调用失控

#### 💾 持久化 Agent 记忆

Encre Agent 拥有独立的记忆系统 —— 随着使用愈加了解你：

- 记忆以带权知识图谱存储 —— 情节、事实、偏好、决策、教训，每个节点都带置信度
- **置信度衰减与强化**：久未使用的记忆自动衰减，任务结果会强化或修正既有信念
- **语义检索**：通过语义理解快速定位相关记忆
- **实体枢纽与交叉关联**：相关记忆按共现与语义相似度自动连线，支持一跳召回

#### 🎯 9 个专业技能

Agent 提供 9 个可调用的技能：

`debug`、`loop`、`stuck`、`code-review`、`refactor`、`gen-test`、`verify`、`write-docs`、`chart-visualization`

**优先级覆盖**：托管（managed）> 用户 > 项目 > 内置 —— 满足你的个性化需求。

#### 🤝 多智能体协作

- **Swarm 系统**：多个 Agent 并发执行，互不干扰
- **共享黑板**：所有 Agent 共享状态，完全透明
- **共识协议**：多个 Agent 对提案投票，选出最佳方案
- **任务规划器**：自动将复杂任务拆解为子任务树
- **6 个内置角色**：每个角色都有专属的提示词与能力

#### 🧱 Encre Agent Core（Rust 原生）

Encre Agent Core —— 性能关键模块由 Rust 实现，带来极致速度与内存安全：

- 极速文件 I/O，支持 offset / limit
- 正则搜索与 Glob 快速定位内容
- SIMD 加速文本匹配（硬件级）
- Unified Diff 精确计算代码差异
- 在安全环境中沙箱化执行 Shell
- 通过 Linux Landlock 进行内核级文件系统限制
- 智能 Token 计数（英文 / 中文 / 数字 / 代码）
- 文本语义相似度比较
- LSP 协议解析（JSON-RPC 2.0）

#### 🖥️ Encre Agent Desktop

基于 Electron + React 19 构建的全功能 AI 聊天桌面应用：

- **聊天界面**：Markdown 渲染、代码高亮、文件附件
- **多会话**：并发聊天，支持分支与模糊搜索
- **设置面板**：模型配置、网关、Agent、MCP 服务器、技能、规则、记忆
- **iClaw 模式**：自动化运行器，支持批量操作
- **内嵌终端**：xterm.js + node-pty，带工作目录浏览
- **代码编辑器**：Monaco Editor，支持 16+ 种语言（TypeScript、JavaScript、Python、Rust、Go、Java、C/C++、PHP、Ruby、Swift、Kotlin、SQL 等）
- **11 种界面语言**：英文、简体中文、繁体中文、日语、韩语、德语、西班牙语、葡萄牙语、土耳其语、阿拉伯语、希伯来语，支持运行时切换
- **Git 集成**：状态与 diff，带缓存
- **系统托盘**：通知 + 语音输入支持
- **加密存储**：AES-256-GCM 加密的浏览器 Cookie
- **跨平台**：Windows（NSIS）、macOS（.pkg）、Linux（.deb / .rpm）

#### 🧩 插件市场与中央仓库

基于 PyPI 中央仓库的开放插件生态——可直接在内置市场安装技能与工具：

- **一个 Wheel，全版本兼容**：所有市场包都是通用纯 Python wheel，支持 **Python 3.11+**——一次构建，处处安装
- **ea-plugin-cli**：插件统一工具链，覆盖脚手架、构建、验证、发布全流程——[ea-plugin-cli](https://github.com/mf2023/ea-plugin-cli)
- **ea-cwh 中央仓库**：以 PyPI 数据包形式分发的权威插件目录，市场 UI 完全离线读取——[ea-cwh](https://github.com/mf2023/ea-cwh)
- **256 个官方插件**：`ea-skill-*` / `ea-tool-*` / `ea-connector-*` 包覆盖数据、媒体、网页、开发、出行、金融与消息平台等领域
- **全链路校验**：sha256 摘要核验、沙箱化安装验证、CI 门禁合并——插件必须经过验证管线才能上架

#### ⚙️ 开发者工具链

- **上下文压缩**：9 种策略，智能管理对话上下文
- **LSP 客户端**：16+ 种语言服务器自动发现，提供实时代码诊断
- **会话记忆索引**：BM25 检索，快速召回历史会话
- **进化系统**：元认知、反射循环、策略优化
- **反馈学习器**：基于 Jaccard 相似度的错误修正
- **交互式 Notebook**：IPython 内核，支持实时执行代码
- **插件系统**：市场驱动的可扩展架构，配备经 CI 校验的插件中央仓库
- **用户画像**：人格推断与灵魂系统
- **规范引擎**：结构化规范文档管理
- **WebSocket 服务器**：实时通信 + 管理 HTTP API

<h2 align="center">🚀 快速开始</h2>

### 3 步开始使用

**步骤 1：克隆仓库**

```bash
git clone https://github.com/Dunimd/encre-agent.git
cd encre-agent
```

**步骤 2：一键构建**

```bash
python build.py
```

一条命令即可构建 Rust 扩展、安装 Python 包并打包桌面应用。

**步骤 3：启动**

```bash
# 桌面应用
cd desktop && npm start

# 或使用 CLI（iClaw 模式）
python -m encre.iclaw --help
```

### 基础用法

通过桌面应用开始与 Agent 对话，或将其接入你常用的聊天平台。配置好 AI 模型提供商和权限模式，即可立即使用。

### 配置

配置以加密 JSON 文件的形式存储在数据目录（`~/.dunimd/encre/config/`）下，通常通过 Encre Agent Desktop 的设置面板管理：

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

`<data_dir>/config/` 下的配置文件：
1. `models.json` —— 模型端点、当前模型、故障转移
2. `agents.json` —— 智能体人格、子智能体、系统提示词、技能
3. `adapters.json` —— 平台适配器凭据
4. `mcp.json` —— MCP 服务器定义
5. `ui.json` —— 界面偏好
6. `runtime.json` —— 行为开关、权限及其它运行时字段

环境变量使用 `ENCRE_` 前缀（如 `ENCRE_DATA_DIR`、`ENCRE_PROFILE`），可覆盖数据目录与配置档案的选择。

<h2 align="center">❓ 常见问题</h2>

**问：Encre Agent 支持哪些 AI 模型？**

答：**51 个提供商**，包括 OpenAI、Anthropic Claude、Google Gemini、DeepSeek、阿里 Qwen、腾讯混元、小米 MiMo、月之暗面 Kimi、智谱 GLM、MiniMax、Ollama、LM Studio、HuggingFace、AWS Bedrock、OpenRouter 等。Encre Agent 是完全原生开发的，不是任何项目的套壳（如 Codex）。

**问：Encre Agent 能做什么？**

答：熟练助手能做的一切 —— 读写编辑代码、运行 shell 命令、打开浏览器做调研、自动化桌面、管理文件、调度任务、与其他 Agent 协作，等等。它内置 **74 个工具**和 **185 个技能包**，几乎可以处理任何任务。

**问：能否在喜欢的聊天平台上使用 Encre Agent？**

答：可以！Encre Agent 支持 **24 个聊天平台**，包括 Telegram、Discord、Slack、钉钉、企业微信、微信、WhatsApp、QQ Bot、邮件、Matrix 等。每个平台都有专属的适配器。

**问：Encre Agent 使用起来安全吗？**

答：安全是头等大事。Encre Agent 提供 **7 种权限模式**，从完全开放到每次操作都需要确认。额外保护还包括 SSRF 防护、Docker 沙箱、Landlock 内核限制以及 AI 风险分类。

**问：Encre Agent 会在会话之间记忆信息吗？**

答：是的。Encre Agent 拥有 **持久化记忆系统**，可存储重要信息、学习你的偏好，并在对话间保持上下文。随着时间推移，它会变得更聪明、更个性化。

**问：多个 Agent 能否协同工作？**

答：当然。Encre Agent 通过 Swarm 系统支持 **多智能体协作**。你可以派生出多个不同角色的 Agent（coder、researcher、critic 等），它们共享信息并协同完成复杂任务。

**问：有桌面应用吗？**

答：有！**Encre Agent Desktop** 是一个全功能的 Electron + React 应用，包含聊天界面、代码编辑器（Monaco）、内嵌终端、文件浏览器、设置面板等。支持 Windows、macOS 和 Linux。

**问：能使用自己的 AI 模型吗？**

答：可以。你可以使用任何 OpenAI 兼容的 API，通过 Ollama / LM Studio 自托管模型，或接入自定义后端。Encre Agent 设计为后端无关。

<h2 align="center">🌏 社区与许可</h2>

- 欢迎提交 Issue 和 PR！
- GitHub：https://github.com/Dunimd/encre-agent
- Gitee：https://gitee.com/dunimd/encre-agent
- GitCode：https://gitcode.com/dunimd/encre-agent

<div align="center">

## 📄 许可证与开源协议

### 项目许可证

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="Apache License 2.0">
  </a>
</p>

本项目采用 **Apache License 2.0**。完整文本请参见 [LICENSE](LICENSE) 文件。

### 依赖许可协议

下表汇总了 **Python 框架**、**Encre Agent Core**（Rust 原生引擎）与 **Encre Agent Desktop**（Electron 应用）所使用的依赖包。版本号与精确的许可文本不在此处固定，请以各上游项目为准。

<div align="center">

| 包名 | 协议 | 包名 | 协议 |
|:-----------|:-----------|:-----------|:-----------|
| httpx | BSD-3-Clause | pydantic | MIT |
| beautifulsoup4 | MIT | markdownify | MIT |
| lxml | BSD-3-Clause | tomli | MIT |
| tomli-w | MIT | pyyaml | MIT |
| cryptography | Apache-2.0 / BSD | zero-api-key-web-search | Apache-2.0 |
| pathspec | MPL-2.0 | websockets | BSD-3-Clause |
| Pillow | Historical | (built-in) | CDP 浏览器引擎 |
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
