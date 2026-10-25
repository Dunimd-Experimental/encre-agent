# Third-Party Acknowledgments

Encre Agent is built on the work of thousands of people — open-source maintainers, contributors, and the communities around them. This document gives credit to the projects and people Encre Agent depends on, explains what licenses we use and why, and tells you how to keep this list accurate as the project grows.

If you maintain one of these projects and want us to fix or remove an entry, visit **conduct.dunimd.com** *(pending activation)*. For security issues with our handling of your code, see [SECURITY.md](SECURITY.md).

---

## Table of Contents

- [Project Credits](#project-credits)
- [Core Runtime](#core-runtime)
- [Frontend (Electron App)](#frontend-electron-app)
- [Backend (Python)](#backend-python)
- [Rust Crates (Native Module)](#rust-crates-native-module)
- [Fonts, Icons, and Assets](#fonts-icons-and-assets)
- [License Texts](#license-texts)
- [How to Update This Document](#how-to-update-this-document)
- [Reporting a License Issue](#reporting-a-license-issue)

---

## Project Credits

### Maintainers

| Name | Role | Contact |
|---|---|---|
| Wenze Wei (WenzeWei) | Project lead, architecture, Encre Agent Core | [weiwenze20212021@outlook.com](mailto:weiwenze20212021@outlook.com) *(see [CONTRIBUTING.md](CONTRIBUTING.md))* |

Maintainer list is updated in [CHANGELOG.md](CHANGELOG.md) when changes happen.

### Inspirations

Encre Agent's design draws on ideas from these adjacent projects — we learned a lot by reading their code and docs, and we gratefully acknowledge them even where we did not copy code:

- **[Claude Code](https://docs.claude.com/en/docs/claude-code)** — permission-mode UX and tool-loop ergonomics
- **[OpenCode](https://opencode.ai)** — LSP-aware tool design and i18n architecture
- **[Aider](https://aider.chat)** — diff-friendly editing workflow
- **[Cody](https://sourcegraph.com/cody)** — context-engineering patterns
- **[Continue](https://continue.dev)** — extensible provider/adapter registry
- **[Cursor](https://www.cursor.com)** — IDE-style interaction model

If we forgot you, please open a PR — see [How to Update This Document](#how-to-update-this-document).

### Language & Region-Specific Thanks

- **Chinese open-source community** — for the model APIs, the chat-platform adapters (Feishu, DingTalk, WeCom, WeChat, QQ Bot, Tencent Yuanbao), and the documentation reviews
- **Hugging Face** — for the `transformers` ecosystem that powers our optional local-mode backend
- **The Rust community** — for `serde`, `pyo3`, and the broader ecosystem that makes a single Rust crate a viable cross-language acceleration layer

---

## Core Runtime

| Project | License | Purpose |
|---|---|---|
| [Electron](https://electronjs.org) | MIT | Desktop application framework |
| [Node.js](https://nodejs.org) | Node.js License ([LICENSE](https://github.com/nodejs/node/blob/main/LICENSE)) | JavaScript runtime |
| [Python](https://python.org) | PSF License | Backend runtime |
| [Rust](https://rust-lang.org) | MIT / Apache 2.0 | Performance-critical components |
| [Cargo](https://doc.rust-lang.org/cargo) | MIT / Apache 2.0 | Rust package manager |

---

## Frontend (Electron App)

### Runtime Dependencies

From [`desktop/package.json`](../desktop/package.json):

| Project | License | Purpose |
|---|---|---|
| [@xterm/xterm](https://xtermjs.org) | MIT | Terminal emulator core |
| [@xterm/addon-fit](https://xtermjs.org) | MIT | Terminal auto-fit addon |
| [@xterm/addon-webgl](https://xtermjs.org) | MIT | Terminal WebGL renderer addon |
| [React](https://react.dev) | MIT | UI framework |
| [React DOM](https://react.dev) | MIT | React renderer for the browser |
| [Monaco Editor](https://microsoft.github.io/monaco-editor) | MIT | Code editor |
| [fuse.js](https://fusejs.io) | Apache 2.0 | Fuzzy search |
| [highlight.js](https://highlightjs.org) | BSD 3-Clause | Syntax highlighting |
| [markdown-it](https://markdown-it.github.io) | MIT | Markdown rendering |
| [node-pty](https://github.com/microsoft/node-pty) | MIT | Pseudo-terminal allocation |
| [simple-icons](https://simpleicons.org) | CC0 1.0 | Icon set |

### Build-Time Dependencies

| Project | License | Purpose |
|---|---|---|
| [electron](https://electronjs.org) | MIT | Desktop app shell |
| [electron-builder](https://www.electron.build) | MIT | Installer / packager (NSIS, pkg, deb, rpm) |
| [esbuild](https://esbuild.github.io) | MIT | TypeScript bundler |
| [TypeScript](https://typescriptlang.org) | Apache 2.0 | Language + type checker |

---

## Backend (Python)

From [`pyproject.toml`](../pyproject.toml). Versions are pinned by the project; the version columns here reflect the lower bound from `pyproject.toml`.

### Core Dependencies

| Project | License | Pinned | Purpose |
|---|---|---|---|
| [httpx](https://www.python-httpx.org) | BSD 3-Clause | `>=0.27` | Async HTTP client used by all backends and the WebSocket server |
| [pydantic](https://docs.pydantic.dev) | MIT | `>=2.5` | Data validation throughout the Agent loop |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | `>=4.12` | HTML/XML parsing for `web_fetch` |
| [markdownify](https://github.com/matthewdeanmartin/markdownify) | MIT | `>=0.12` | HTML → Markdown conversion |
| [lxml](https://lxml.de) | BSD 3-Clause | `>=5.1` | XML/HTML processing |
| [tomli](https://github.com/hukkin/tomli) | MIT | `>=2.0` | TOML parser (Python 3.11 stdlib fallback) |
| [tomli-w](https://github.com/catherinettt/tomli-w) | MIT | `>=1.0` | TOML writer |
| [PyYAML](https://pyyaml.org/) | MIT | `>=6.0` | YAML config parsing |
| [cryptography](https://cryptography.io) | Apache 2.0 / BSD | `>=41.0` | Encryption primitives (AES-GCM, Fernet) |
| [zero-api-key-web-search](https://pypi.org/project/zero-api-key-web-search/) | Apache 2.0 | `>=23.0` | Default `web_search` backend |
| [websockets](https://websockets.readthedocs.io) | BSD 3-Clause | `>=12.0,<14` | WebSocket client/server |
| [Pillow](https://python-pillow.org) | HPND | `>=10.0` | Image processing for the `image` tool |
| [tiktoken](https://github.com/openai/tiktoken) | MIT | `>=0.5` | Tokenization for token counting |
| [numpy](https://numpy.org) | BSD 3-Clause | `>=1.24` | Numerical computing (memory similarity, embedding search) |
| [loguru](https://loguru.com) | MIT | `>=0.7` | Logging |
| [mss](https://github.com/BoboPypy/mss) | MIT | `>=9.0` | Screen capture for desktop automation |
| [openpyxl](https://openpyxl.readthedocs.io) | MIT | `>=3.1` | Excel file processing |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | MIT | `>=0.10` | PDF text extraction |
| [pyautogui](https://pyautogui.readthedocs.io) | BSD 3-Clause | `>=0.9` | Cross-platform GUI automation |
| [pypdf](https://pypdf.readthedocs.io) | BSD 3-Clause | `>=4.0` | Modern PDF processing |
| [PyPDF2](https://pypdf2.readthedocs.io) | BSD 3-Clause | `>=3.0` | Legacy PDF processing (kept for backward compatibility) |
| [pytesseract](https://github.com/madmaze/pytesseract) | Apache 2.0 | `>=0.3` | OCR via Tesseract |
| [uiautomation](https://github.com/pywinauto/uiautomation) | MIT | `>=2.0` | Windows UI automation |
| [watchfiles](https://github.com/samuelcolvin/watchfiles) | MIT | `>=0.21` | File-system watcher |

### Optional Dependencies

Install with `pip install -e ".[<extra>]"` — see [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup).

| Extra | Project | License | Purpose |
|---|---|---|---|
| `anthropic` | [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT | Anthropic Claude backend |
| `ollama` | [ollama](https://github.com/ollama/ollama-python) | MIT | Ollama local-model backend |
| `native` | [encre-native](https://pypi.org/project/encre-native/) | Apache 2.0 | Bundled pre-built Rust extension (`encre._native`) |
| `aiohttp` | [aiohttp](https://docs.aiohttp.org) | Apache 2.0 | Async HTTP server backend for `rest_client` |
| `discord` | [discord.py](https://github.com/Rapptz/discord.py) | MIT | Discord adapter |
| `slack` | [slack_bolt](https://github.com/slackapi/bolt-python) + [slack_sdk](https://github.com/slackapi/python-slack-sdk) | MIT | Slack adapter |
| `telegram` | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | GPL-3.0 | Telegram adapter |
| `dingtalk` | [dingtalk-stream](https://github.com/dingtalk-stream/dingtalk-stream-sdk-python) | MIT | 钉钉 adapter |
| `email` | [aioimaplib](https://github.com/aioimaplib/aioimaplib) + [aiosmtplib](https://aiosmtplib.readthedocs.io) | Apache 2.0 / BSD 3-Clause | Email (IMAP + SMTP) adapter |
| `local` | [PyTorch](https://pytorch.org/) + [Transformers](https://huggingface.co/docs/transformers) | BSD 3-Clause / Apache 2.0 | Local HuggingFace model backend |
| `aws` | [boto3](https://github.com/boto/boto3) | Apache 2.0 | AWS Bedrock backend |
| `tracing` | [opentelemetry-api](https://opentelemetry.io/) + [opentelemetry-sdk](https://opentelemetry.io/) + [opentelemetry-exporter-otlp-proto-grpc](https://opentelemetry.io/) | Apache 2.0 | OpenTelemetry / OpenInference tracing |

### Development Dependencies

Installed with `pip install -e ".[dev]"`.

| Project | License | Purpose |
|---|---|---|
| [pytest](https://docs.pytest.org) | MIT | Test runner |
| [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache 2.0 | Async test support |
| [ruff](https://docs.astral.sh/ruff/) | MIT | Linter + formatter |
| [mypy](https://mypy-lang.org/) | MIT | Static type checker |
| [pre-commit](https://pre-commit.com/) | MIT | Git-hook framework |

---

## Rust Crates (Native Module)

From [`native/crates/encre-core/Cargo.toml`](../native/crates/encre-core/Cargo.toml) and [`native/crates/encre-py/Cargo.toml`](../native/crates/encre-py/Cargo.toml).

### `encre-core` (the native library — `crate name = "encre"`)

| Crate | License | Purpose |
|---|---|---|
| [serde](https://serde.rs) | MIT / Apache 2.0 | Serialization framework with `derive` support |
| [serde_json](https://docs.rs/serde_json) | MIT / Apache 2.0 | JSON serialization |
| [regex](https://docs.rs/regex) | MIT / Apache 2.0 | Regular expressions (used by `search.rs`) |
| [walkdir](https://docs.rs/walkdir) | MIT / Apache 2.0 | Recursive directory traversal |
| [similar](https://docs.rs/similar) | Apache 2.0 | Text diffing (used by `diff.rs`) |
| [glob](https://docs.rs/glob) | MIT / Apache 2.0 | Glob pattern matching |
| [wide](https://github.com/starkat99/wide-rs) | MIT / Apache 2.0 | SIMD intrinsics — **optional**, behind the `simd` feature |
| [windows-sys](https://github.com/microsoft/windows-rs) | MIT / Apache 2.0 | Windows API bindings (Windows targets only) |
| [libc](https://github.com/rust-lang/libc) | MIT / Apache 2.0 | libc bindings (non-Windows targets only) |

### `encre-py` (PyO3 bindings — exports `encre._native`)

| Crate | License | Purpose |
|---|---|---|
| [encre](../native/crates/encre-core/) (path) | Apache 2.0 | The native core crate above |
| [pyo3](https://github.com/PyO3/pyo3) | MIT / Apache 2.0 | Python bindings with `abi3-py39` |
| [serde_json](https://docs.rs/serde_json) | MIT / Apache 2.0 | JSON for the Python↔Rust boundary |

---

## Fonts, Icons, and Assets

- **Logos and brand marks** for the platforms listed under [Inspirations](#inspirations) are property of their respective owners and used here for descriptive purposes only.
- **Default UI font** is the system stack on each platform; Encre Agent does not bundle any web fonts.
- **Code-editor icon set** uses [Monaco Editor's built-in iconography](https://microsoft.github.io/monaco-editor).
- **Chat-platform brand icons** use [simple-icons](https://simpleicons.org) (CC0 1.0).

If you would like a brand icon added or removed, see [Reporting a License Issue](#reporting-a-license-issue).

---

## License Texts

The full text of each license used by dependencies in this project follows. They are reproduced verbatim from the SPDX distribution. Where a license is long, we link to the canonical source rather than embedding — please open an issue if a license you expected to see is missing.

### Apache License 2.0

Several components are licensed under the Apache License, Version 2.0. You may obtain a copy of the License at <https://www.apache.org/licenses/LICENSE-2.0>. A copy is also included in the [`LICENSE`](../LICENSE) file at the root of this repository.

### MIT License

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

### BSD 3-Clause License

> Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
> 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.

### BSD 2-Clause License

> Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.

### HPND (Historical Permission Notice and Disclaimer)

Used by [Pillow](https://python-pillow.org). The full text is at <https://spdx.org/licenses/HPND.html>.

> *Historical Permission Notice and Disclaimer*
>
> Copyright notice required for this license is the same as the standard MIT License text, with the addition of the following disclaimer: the software is provided "as-is" without warranty of any kind, and the copyright holder shall not be liable for any damages arising from the use of the software.

### ISC License

Used by some transitive Node dependencies. The full text is at <https://spdx.org/licenses/ISC.html>.

### CC0 1.0 Universal (Public Domain Dedication)

Used by [simple-icons](https://simpleicons.org). The full text is at <https://creativecommons.org/publicdomain/zero/1.0/>.

### GNU GPL-3.0 / LGPL-3.0

[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) is licensed under GPL-3.0. This is a copyleft license; if you distribute a binary that links against it, you must also release your source under a compatible license. Encre Agent itself is **not** GPL-licensed; the Telegram adapter is dynamically loaded and can be omitted by simply not installing the `telegram` extra.

- GPL-3.0: <https://www.gnu.org/licenses/gpl-3.0.html>
- LGPL-3.0: <https://www.gnu.org/licenses/lgpl-3.0.html>

If you want to ship a binary that does **not** trigger GPL obligations, do not install the `telegram` extra (or strip the Telegram adapter from your build):

```bash
# Omit telegram adapter from a binary build
pip install -e ".[all_except_telegram]"
```

### Node.js License

Node.js is licensed under a custom MIT-like license. The full text is at <https://github.com/nodejs/node/blob/main/LICENSE>.

---

## How to Update This Document

This file is **manually curated** — not auto-generated. When you add a dependency, you must update the relevant table here in the same PR. Here is the checklist:

### Before Adding a New Dependency

- [ ] **Check license compatibility** with Apache 2.0 (our license). Compatible: MIT, BSD-2-Clause, BSD-3-Clause, ISC, Apache-2.0, MPL-2.0, CC0-1.0, HPND, Unlicense. **Incompatible**: GPL-2.0+, AGPL-3.0, SSPL, BUSL, Commons Clause, unlicensed code. LGPL-3.0 is *dynamic-linking compatible* but a binary that ships the LGPL component statically may trigger source-release obligations. When in doubt, ask on the issue first.
- [ ] **Pin a version range** (`>=X.Y,<Z+1`) in `pyproject.toml`, `Cargo.toml`, or `package.json`.
- [ ] **Run the audit tool** (`cargo audit`, `npm audit`, `pip-audit`) on the new dep.
- [ ] **Decide whether it's optional.** Most new things should be in an `extras_require` group, not in core.

### When Adding to `pyproject.toml`

Add a row to the **Backend (Python) → Core Dependencies** or **Optional Dependencies** table, including:

- Project name (linked to the upstream URL)
- License (look it up on the package's PyPI page, do not guess)
- Version constraint
- One-sentence purpose

### When Adding to `desktop/package.json`

Add a row to **Frontend (Electron App) → Runtime Dependencies** or **Build-Time Dependencies**.

### When Adding to `native/crates/*/Cargo.toml`

Add a row to the matching **`encre-core`** or **`encre-py`** table under **Rust Crates (Native Module)**.

### When Removing a Dependency

Don't just delete it from the manifest — also delete the row from this file in the same commit. Stale rows here have misled contributors before.

### Verification Script

This file is consistent with the manifests as of release **0.5.0**. To re-verify after editing:

```bash
# Compare Python core deps with pyproject.toml
python - <<'PY'
import tomllib, re, pathlib
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

Re-run before opening a release PR. Add a CI job for this in `.github/workflows/build-binary.yml` if you want it enforced.

---

## Reporting a License Issue

If you are the maintainer of a project listed here and want us to:

- **Fix a license attribution** (wrong name, missing copyright line, etc.)
- **Remove the entry** (your project moved or relicensed)
- **Add a missing dependency** we forgot to credit
- **Investigate a license concern** (you believe we are using your code in a way that violates your license)

Visit **conduct.dunimd.com** *(pending activation)* with:

1. The project name and the row(s) affected
2. What the issue is
3. Your suggested fix
4. A link to your canonical license text

We commit to:

- Acknowledge your report within **5 business days**
- Ship a fix or document why not within **30 days**
- Publish a CHANGELOG entry crediting the correction

If you believe we are in violation of your license, see [SECURITY.md → Reporting a Vulnerability](SECURITY.md#reporting-a-vulnerability) — the same address can be used, just mark the subject line clearly.

---

## Acknowledgments

This document was last reviewed against the project manifests on **2026-08-23** for the **0.5.0** release. The dependency lists above match `pyproject.toml`, `desktop/package.json`, and `native/crates/*/Cargo.toml` at that commit.

If you find a discrepancy, please open a PR — see [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup).
