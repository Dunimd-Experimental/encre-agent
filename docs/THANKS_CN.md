# 第三方致谢

Encre Agent 建立在数千位开源维护者、贡献者以及他们所处社区的工作之上。本文档向 Encre Agent 所依赖的项目与人致谢，说明我们使用了哪些许可证以及原因，并告诉你如何在项目演进过程中保持这份清单的准确性。

如果你是下列某个项目的维护者并希望我们更正或移除某条目，请访问 **conduct.dunimd.com** *（pending activation）*。如涉及我们对你方代码处理方式的安全问题，请参见 [SECURITY.md](SECURITY.md)。

---

## 目录

- [项目致谢](#项目致谢)
- [核心运行时](#核心运行时)
- [前端（Electron 应用）](#前端electron-应用)
- [后端（Python）](#后端python)
- [Rust 库（原生模块）](#rust-库原生模块)
- [字体、图标与素材](#字体图标与素材)
- [许可证文本](#许可证文本)
- [如何更新本文档](#如何更新本文档)
- [报告许可证问题](#报告许可证问题)

---

## 项目致谢

### 维护者

| 姓名 | 角色 | 联系方式 |
|---|---|---|
| 卫文泽 (WenzeWei) | 项目负责人、架构、Rust 内核 | [weiwenze20212021@outlook.com](mailto:weiwenze20212021@outlook.com) *(参见 [CONTRIBUTING.md](CONTRIBUTING.md))* |

维护者名单的变更会记录在 [CHANGELOG.md](CHANGELOG.md) 中。

### 设计灵感

Encre Agent 的设计借鉴了以下相邻项目的思想 —— 通过阅读它们的代码与文档我们获益良多，即使没有直接复制代码，也在此表达感谢：

- **[Claude Code](https://docs.claude.com/en/docs/claude-code)** — 权限模式 UX 与工具循环的交互设计
- **[OpenCode](https://opencode.ai)** — LSP 工具设计思路与国际化架构
- **[Aider](https://aider.chat)** — diff 友好的编辑工作流
- **[Cody](https://sourcegraph.com/cody)** — 上下文工程模式
- **[Continue](https://continue.dev)** — 可扩展的 provider/adapter 注册表
- **[Cursor](https://www.cursor.com)** — IDE 风格的交互模型

如果你或你的项目未被列出，请直接提交 PR —— 参见 [如何更新本文档](#如何更新本文档)。

### 语言与区域致谢

- **中文开源社区** — 提供了模型 API、聊天平台适配器（飞书、钉钉、企业微信、微信、QQ Bot、腾讯元宝）以及文档审阅
- **Hugging Face** — 提供了 `transformers` 生态，使我们的本地模式后端得以实现
- **Rust 社区** — 提供了 `serde`、`pyo3` 等关键库，使单个 Rust crate 能够作为可行的跨语言加速层

---

## 核心运行时

| 项目 | 许可证 | 用途 |
|---|---|---|
| [Electron](https://electronjs.org) | MIT | 桌面应用框架 |
| [Node.js](https://nodejs.org) | Node.js License ([LICENSE](https://github.com/nodejs/node/blob/main/LICENSE)) | JavaScript 运行时 |
| [Python](https://python.org) | PSF License | 后端运行时 |
| [Rust](https://rust-lang.org) | MIT / Apache 2.0 | 性能关键组件 |
| [Cargo](https://doc.rust-lang.org/cargo) | MIT / Apache 2.0 | Rust 包管理器 |

---

## 前端（Electron 应用）

### 运行时依赖

来自 [`desktop/package.json`](../desktop/package.json)：

| 项目 | 许可证 | 用途 |
|---|---|---|
| [@xterm/xterm](https://xtermjs.org) | MIT | 终端模拟器核心 |
| [@xterm/addon-fit](https://xtermjs.org) | MIT | 终端自适应 addon |
| [@xterm/addon-webgl](https://xtermjs.org) | MIT | 终端 WebGL 渲染 addon |
| [React](https://react.dev) | MIT | UI 框架 |
| [React DOM](https://react.dev) | MIT | React 浏览器端渲染器 |
| [Monaco Editor](https://microsoft.github.io/monaco-editor) | MIT | 代码编辑器 |
| [fuse.js](https://fusejs.io) | Apache 2.0 | 模糊搜索 |
| [highlight.js](https://highlightjs.org) | BSD 3-Clause | 语法高亮 |
| [markdown-it](https://markdown-it.github.io) | MIT | Markdown 渲染 |
| [node-pty](https://github.com/microsoft/node-pty) | MIT | 伪终端分配 |
| [simple-icons](https://simpleicons.org) | CC0 1.0 | 图标库 |

### 构建期依赖

| 项目 | 许可证 | 用途 |
|---|---|---|
| [electron](https://electronjs.org) | MIT | 桌面应用外壳 |
| [electron-builder](https://www.electron.build) | MIT | 安装包/打包器（NSIS、pkg、deb、rpm） |
| [esbuild](https://esbuild.github.io) | MIT | TypeScript 打包器 |
| [TypeScript](https://typescriptlang.org) | Apache 2.0 | 语言与类型检查器 |

---

## 后端（Python）

来自 [`pyproject.toml`](../pyproject.toml)。版本下限取自 `pyproject.toml`。

### 核心依赖

| 项目 | 许可证 | 最低版本 | 用途 |
|---|---|---|---|
| [httpx](https://www.python-httpx.org) | BSD 3-Clause | `>=0.27` | 所有后端与 WebSocket 服务器使用的异步 HTTP 客户端 |
| [pydantic](https://docs.pydantic.dev) | MIT | `>=2.5` | Agent 循环中的数据验证 |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | `>=4.12` | `web_fetch` 工具的 HTML/XML 解析 |
| [markdownify](https://github.com/matthewdeanmartin/markdownify) | MIT | `>=0.12` | HTML → Markdown 转换 |
| [lxml](https://lxml.de) | BSD 3-Clause | `>=5.1` | XML/HTML 处理 |
| [tomli](https://github.com/hukkin/tomli) | MIT | `>=2.0` | TOML 解析（Python 3.11 stdlib 兜底） |
| [tomli-w](https://github.com/catherinettt/tomli-w) | MIT | `>=1.0` | TOML 写入 |
| [PyYAML](https://pyyaml.org/) | MIT | `>=6.0` | YAML 配置解析 |
| [cryptography](https://cryptography.io) | Apache 2.0 / BSD | `>=41.0` | 加密原语（AES-GCM、Fernet） |
| [zero-api-key-web-search](https://pypi.org/project/zero-api-key-web-search/) | Apache 2.0 | `>=23.0` | 默认 `web_search` 后端 |
| [websockets](https://websockets.readthedocs.io) | BSD 3-Clause | `>=12.0,<14` | WebSocket 客户端/服务器 |
| [Pillow](https://python-pillow.org) | HPND | `>=10.0` | `image` 工具的图像处理 |
| [tiktoken](https://github.com/openai/tiktoken) | MIT | `>=0.5` | Token 计数 |
| [numpy](https://numpy.org) | BSD 3-Clause | `>=1.24` | 数值计算（记忆相似度、embedding 检索） |
| [loguru](https://loguru.com) | MIT | `>=0.7` | 日志 |
| [mss](https://github.com/BoboPypy/mss) | MIT | `>=9.0` | 桌面自动化的屏幕截图 |
| [openpyxl](https://openpyxl.readthedocs.io) | MIT | `>=3.1` | Excel 文件处理 |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | MIT | `>=0.10` | PDF 文本提取 |
| [pyautogui](https://pyautogui.readthedocs.io) | BSD 3-Clause | `>=0.9` | 跨平台 GUI 自动化 |
| [pypdf](https://pypdf.readthedocs.io) | BSD 3-Clause | `>=4.0` | 现代 PDF 处理 |
| [PyPDF2](https://pypdf2.readthedocs.io) | BSD 3-Clause | `>=3.0` | 旧版 PDF 处理（保留以兼容） |
| [pytesseract](https://github.com/madmaze/pytesseract) | Apache 2.0 | `>=0.3` | 基于 Tesseract 的 OCR |
| [uiautomation](https://github.com/pywinauto/uiautomation) | MIT | `>=2.0` | Windows UI 自动化 |
| [watchfiles](https://github.com/samuelcolvin/watchfiles) | MIT | `>=0.21` | 文件系统监听 |

### 可选依赖

通过 `pip install -e ".[<extra>]"` 安装 — 参见 [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup)。

| Extra | 项目 | 许可证 | 用途 |
|---|---|---|---|
| `anthropic` | [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT | Anthropic Claude 后端 |
| `ollama` | [ollama](https://github.com/ollama/ollama-python) | MIT | Ollama 本地模型后端 |
| `native` | [encre-native](https://pypi.org/project/encre-native/) | Apache 2.0 | 内置预构建 Rust 扩展（`encre._native`） |
| `aiohttp` | [aiohttp](https://docs.aiohttp.org) | Apache 2.0 | `rest_client` 的异步 HTTP 后端 |
| `discord` | [discord.py](https://github.com/Rapptz/discord.py) | MIT | Discord 适配器 |
| `slack` | [slack_bolt](https://github.com/slackapi/bolt-python) + [slack_sdk](https://github.com/slackapi/python-slack-sdk) | MIT | Slack 适配器 |
| `telegram` | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | GPL-3.0 | Telegram 适配器 |
| `dingtalk` | [dingtalk-stream](https://github.com/dingtalk-stream/dingtalk-stream-sdk-python) | MIT | 钉钉适配器 |
| `email` | [aioimaplib](https://github.com/aioimaplib/aioimaplib) + [aiosmtplib](https://aiosmtplib.readthedocs.io) | Apache 2.0 / BSD 3-Clause | 邮件（IMAP + SMTP）适配器 |
| `local` | [PyTorch](https://pytorch.org/) + [Transformers](https://huggingface.co/docs/transformers) | BSD 3-Clause / Apache 2.0 | 本地 HuggingFace 模型后端 |
| `aws` | [boto3](https://github.com/boto/boto3) | Apache 2.0 | AWS Bedrock 后端 |
| `tracing` | [opentelemetry-api](https://opentelemetry.io/) + [opentelemetry-sdk](https://opentelemetry.io/) + [opentelemetry-exporter-otlp-proto-grpc](https://opentelemetry.io/) | Apache 2.0 | OpenTelemetry / OpenInference tracing |

### 开发依赖

通过 `pip install -e ".[dev]"` 安装。

| 项目 | 许可证 | 用途 |
|---|---|---|
| [pytest](https://docs.pytest.org) | MIT | 测试运行器 |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache 2.0 | 异步测试支持 |
| [ruff](https://docs.astral.sh/ruff/) | MIT | Linter + 格式化器 |
| [mypy](https://mypy-lang.org/) | MIT | 静态类型检查 |
| [pre-commit](https://pre-commit.com/) | MIT | Git-hook 框架 |

---

## Rust 库（原生模块）

来自 [`native/crates/encre-core/Cargo.toml`](../native/crates/encre-core/Cargo.toml) 和 [`native/crates/encre-py/Cargo.toml`](../native/crates/encre-py/Cargo.toml)。

### `encre-core`（原生库 — `crate name = "encre"`）

| Crate | 许可证 | 用途 |
|---|---|---|
| [serde](https://serde.rs) | MIT / Apache 2.0 | 带 `derive` 的序列化框架 |
| [serde_json](https://docs.rs/serde_json) | MIT / Apache 2.0 | JSON 序列化 |
| [regex](https://docs.rs/regex) | MIT / Apache 2.0 | 正则表达式（用于 `search.rs`） |
| [walkdir](https://docs.rs/walkdir) | MIT / Apache 2.0 | 递归目录遍历 |
| [similar](https://docs.rs/similar) | Apache 2.0 | 文本 diff（用于 `diff.rs`） |
| [glob](https://docs.rs/glob) | MIT / Apache 2.0 | Glob 模式匹配 |
| [wide](https://github.com/starkat99/wide-rs) | MIT / Apache 2.0 | SIMD 内置函数 — **可选**，由 `simd` feature 启用 |
| [windows-sys](https://github.com/microsoft/windows-rs) | MIT / Apache 2.0 | Windows API 绑定（仅 Windows 目标） |
| [libc](https://github.com/rust-lang/libc) | MIT / Apache 2.0 | libc 绑定（非 Windows 目标） |

### `encre-py`（PyO3 绑定 — 导出 `encre._native`）

| Crate | 许可证 | 用途 |
|---|---|---|
| [encre](../native/crates/encre-core/)（path） | Apache 2.0 | 上面的原生核心 crate |
| [pyo3](https://github.com/PyO3/pyo3) | MIT / Apache 2.0 | Python 绑定，支持 `abi3-py39` |
| [serde_json](https://docs.rs/serde_json) | MIT / Apache 2.0 | Python ↔ Rust 边界的 JSON |

---

## 字体、图标与素材

- **Logo 与品牌标识** —— 灵感来源中的各平台 Logo 归各自所有者所有，此处仅作说明用途
- **默认 UI 字体** —— 使用各平台的系统字体栈；Encre Agent 不内嵌任何 Web 字体
- **代码编辑器图标集** —— 使用 [Monaco Editor 内置图标](https://microsoft.github.io/monaco-editor)
- **聊天平台品牌图标** —— 使用 [simple-icons](https://simpleicons.org)（CC0 1.0）

如希望新增或移除某个品牌图标，请参见 [报告许可证问题](#报告许可证问题)。

---

## 许可证文本

下面是本项目依赖所使用许可证的完整文本。文本均逐字复制自 SPDX 发行版。对较长的许可证，我们给出权威链接而非内嵌 —— 如发现缺少你期望看到的许可证，请提交 issue。

### Apache License 2.0

部分组件基于 Apache License, Version 2.0 授权。你可以在 <https://www.apache.org/licenses/LICENSE-2.0> 获取许可证副本，仓库根目录的 [`LICENSE`](../LICENSE) 文件中也包含完整副本。

### MIT License

> 特此免费授予任何获得本软件副本及相关文档文件（"软件"）的人不受限制地处理本软件的权利，包括但不限于使用、复制、修改、合并、发布、分发、再许可和/或出售软件副本的权利，并允许被授予软件的人这样做，但须满足以下条件：
>
> 上述版权声明和本许可声明应包含在软件的所有副本或实质性部分中。
>
> 本软件按"原样"提供，不附带任何形式的明示或暗示的担保，包括但不限于适销性、特定用途适用性和不侵权的暗示担保。在任何情况下，作者或版权持有人均不对因软件或软件的使用或其他交易而引起的任何索赔、损害或其他责任负责，无论是基于合同、侵权还是其他法律理论。

### BSD 3-Clause License

> 在满足以下条件的前提下，允许源代码和二进制形式的再分发和使用（无论是否修改）：
>
> 1. 源代码的再分发必须保留上述版权声明、本条件列表和以下免责声明。
> 2. 二进制形式的再分发必须在随分发提供的文档和/或其他材料中复制上述版权声明、本条件列表和以下免责声明。
> 3. 未经版权持有人或贡献者的事先书面许可，不得使用其名称来认可或推广从本软件衍生的产品。
>
> 本软件由版权持有人和贡献者按"原样"提供，不附带任何明示或暗示的担保，包括但不限于适销性和特定用途适用性的暗示担保。

### BSD 2-Clause License

> 在满足以下条件的前提下，允许源代码和二进制形式的再分发和使用（无论是否修改）：
>
> 1. 源代码的再分发必须保留上述版权声明、本条件列表和以下免责声明。
> 2. 二进制形式的再分发必须在随分发提供的文档和/或其他材料中复制上述版权声明、本条件列表和以下免责声明。
>
> 本软件由版权持有人和贡献者按"原样"提供，不附带任何明示或暗示的担保，包括但不限于适销性和特定用途适用性的暗示担保。

### HPND（历史许可声明与免责声明）

由 [Pillow](https://python-pillow.org) 使用。完整文本见 <https://spdx.org/licenses/HPND.html>。

> *Historical Permission Notice and Disclaimer*
>
> 本许可证要求的版权声明与标准 MIT 许可证文本相同，并附加以下免责声明：本软件按"原样"提供，不附带任何形式的担保，版权持有人不对因使用本软件而产生的任何损害承担责任。

### ISC License

部分传递性 Node 依赖使用。完整文本见 <https://spdx.org/licenses/ISC.html>。

### CC0 1.0 Universal（公共领域贡献声明）

由 [simple-icons](https://simpleicons.org) 使用。完整文本见 <https://creativecommons.org/publicdomain/zero/1.0/>。

### GNU GPL-3.0 / LGPL-3.0

[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 以 GPL-3.0 授权。这是一种 copyleft 许可证：如果你发布的二进制动态链接了它，你也必须以兼容许可证发布你的源码。Encre Agent 本身**不是** GPL 授权；Telegram 适配器是动态加载的，只需不安装 `telegram` extra 即可彻底排除。

- GPL-3.0：<https://www.gnu.org/licenses/gpl-3.0.html>
- LGPL-3.0：<https://www.gnu.org/licenses/lgpl-3.0.html>

若你希望发布不触发 GPL 义务的二进制，请不要安装 `telegram` extra（或在构建中剥离 Telegram 适配器）：

```bash
# 在二进制构建中排除 telegram 适配器
pip install -e ".[all_except_telegram]"
```

### Node.js License

Node.js 使用类 MIT 的自定义许可证。完整文本见 <https://github.com/nodejs/node/blob/main/LICENSE>。

---

## 如何更新本文档

本文件**为人工维护**，非自动生成。添加新依赖时，必须在同一 PR 中更新此处的对应表格。检查清单如下：

### 添加新依赖之前

- [ ] **检查许可证与 Apache 2.0（我们的许可证）的兼容性**。兼容：MIT、BSD-2-Clause、BSD-3-Clause、ISC、Apache-2.0、MPL-2.0、CC0-1.0、HPND、Unlicense。**不兼容**：GPL-2.0+、AGPL-3.0、SSPL、BUSL、Commons Clause、未授权代码。LGPL-3.0 与动态链接兼容，但静态链接 LGPL 组件的二进制可能触发源码公开义务。如有疑问，先在 issue 中提出。
- [ ] **在 `pyproject.toml`、`Cargo.toml` 或 `package.json` 中固定版本范围**（`>=X.Y,<Z+1`）。
- [ ] **对新依赖运行审计工具**（`cargo audit`、`npm audit`、`pip-audit`）。
- [ ] **决定是否可选**。多数新东西应该放进 `extras_require`，而不是核心。

### 添加到 `pyproject.toml`

在 **后端（Python）→ 核心依赖** 或 **可选依赖** 表中新增一行，包含：

- 项目名（链接到上游 URL）
- 许可证（在 PyPI 项目页查询，不要猜）
- 版本约束
- 一句话用途

### 添加到 `desktop/package.json`

在 **前端（Electron 应用）→ 运行时依赖** 或 **构建期依赖** 表中新增一行。

### 添加到 `native/crates/*/Cargo.toml`

在 **Rust 库（原生模块）** 下相应的 **`encre-core`** 或 **`encre-py`** 表中新增一行。

### 移除依赖时

不要只从清单中删除 —— 也要在同一 commit 中删除此文件中的对应行。此处的陈旧条目曾误导过贡献者。

### 验证脚本

本文件与发布 **0.5.0** 的清单一致。编辑后可这样重新核对：

```bash
# 比较 Python 核心依赖与 pyproject.toml
python - <<'PY'
import tomllib, pathlib
manifest = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
md = pathlib.Path("docs/THANKS.md").read_text()
core = manifest["project"]["dependencies"]
extras = manifest["project"].get("optional-dependencies", {})
all_pkgs = core + sum(extras.values(), [])
missing = [d.split(">=")[0].split("==")[0].split("[")[0].lower()
           for d in all_pkgs
           if d.split(">=")[0].split("==")[0].split("[")[0].lower() not in md.lower()]
print("Missing from THANKS.md:", missing or "(none)")
PY
```

在发布 PR 前重跑。如希望强制执行，可在 `.github/workflows/build-binary.yml` 中加一个 CI job。

---

## 报告许可证问题

如果你是本文件所列某个项目的维护者，并希望我们：

- **更正许可证归属**（名称错误、版权行缺失等）
- **移除条目**（项目迁移或更换许可证）
- **补充缺失的依赖**
- **调查许可证疑虑**（你认为我们使用你方代码的方式违反了许可证）

请访问 **conduct.dunimd.com** *（pending activation）*，内容包含：

1. 项目名称与受影响的行
2. 问题描述
3. 你的建议修复方案
4. 指向你方权威许可证文本的链接

我们承诺：

- **5 个工作日内**确认你的报告
- **30 天内**发布修复或说明无法修复的原因
- 在 [CHANGELOG.md](CHANGELOG.md) 中发布条目，致谢更正者

如果你认为我们违反了你们的许可证，请参见 [SECURITY.md → Reporting a Vulnerability](SECURITY.md#reporting-a-vulnerability) —— 同一个邮箱即可使用，请在主题行明确标注。

---

## 致谢

本文件已于 **2026-08-23** 针对 **0.5.0** 发布与项目清单完成最后一次核对。上面的依赖列表与该 commit 的 `pyproject.toml`、`desktop/package.json`、`native/crates/*/Cargo.toml` 一致。

如发现差异，请提交 PR —— 参见 [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup)。
