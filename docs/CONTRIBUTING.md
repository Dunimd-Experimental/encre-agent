# Contributing to Encre Agent

Welcome — and thank you for investing time in Encre Agent. This guide is written so that **you, as a developer, know exactly three things when you finish reading it**: what you can safely change, what you are agreeing to when you submit a contribution, and how to get your change merged without unnecessary back-and-forth.

Encre Agent is a single repository that ships three products on one release line:

- a **Python** AI Agent framework (`backend/encre/`)
- a **Rust** native core that backs it (`native/crates/`)
- an **Electron + React 19** Encre Agent Desktop (`desktop/`)

All three share the same version, the same `master` branch, and the same release notes. If you only need to touch one layer, that's fine — the other layers will still build around your change.

---

## Table of Contents

- [Your Rights as a Contributor](#your-rights-as-a-contributor)
- [Quick Decision Tree](#quick-decision-tree)
- [Reporting Bugs](#reporting-bugs)
- [Suggesting Enhancements](#suggesting-enhancements)
- [Pull Request Process](#pull-request-process)
- [Development Setup](#development-setup)
- [Building](#building)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Commit Messages](#commit-messages)
- [Project Structure](#project-structure)
- [Release & Versioning](#release--versioning)
- [Becoming a Maintainer](#becoming-a-maintainer)
- [Communication Channels](#communication-channels)
- [Security Disclosures](#security-disclosures)
- [License](#license)

---

## Your Rights as a Contributor

Before you write a single line, here is the deal — in plain language, not legalese:

- **You keep the copyright** to everything you write. Submitting a pull request does **not** transfer ownership to the Encre Agent project or to any individual maintainer.
- **You grant the project a license to use your work.** By opening a pull request, you agree to release your contribution under the [Apache License 2.0](../LICENSE), the same license the rest of the codebase uses. Other contributors and downstream users can then use your code under the same terms everyone else enjoys.
- **You will be credited.** Substantive contributions are listed in [CHANGELOG.md](CHANGELOG.md) and in the release notes of the version where they land, with a link to your GitHub/Gitee profile.
- **You can withdraw or relicense at any time** for future use of your work, by emailing the maintainers (see [Communication Channels](#communication-channels)). Already-released versions remain under Apache 2.0 — that is the nature of open source — but future revisions can be adjusted.
- **You are not signing a CLA.** There is no Contributor License Agreement on file. Apache 2.0's explicit copyright + license notice is sufficient.
- **You will be told why** if your PR is not accepted. Maintainers close PRs with a reason and, when possible, a concrete next step. Silence for more than 14 days means it was lost in the queue — please ping.
- **You are protected by the [Code of Conduct](CODE_OF_CONDUCT.md).** The CoC applies to maintainers and contributors equally. Report violations privately to the contacts listed in [SECURITY.md](SECURITY.md).

If anything in this section is unclear or you have a special situation (employer-owned work, prior agreement with another project, etc.), open a draft PR or contact the maintainers **before** you write code. It is far cheaper to sort out IP up front than to revert a merge.

---

## Quick Decision Tree

Pick the one that matches what you want to do, and jump to the linked section:

| You want to … | Go to |
|---|---|
| Report something that doesn't work | [Reporting Bugs](#reporting-bugs) |
| Propose a new feature or improvement | [Suggesting Enhancements](#suggesting-enhancements) |
| Fix a typo, doc error, or small bug (< ~30 lines, no API change) | [Pull Request Process](#pull-request-process) → *Small fixes* |
| Add a new backend, tool, adapter, or skill | [Pull Request Process](#pull-request-process) → *New module* |
| Change a public API or the Agent loop | [Pull Request Process](#pull-request-process) → *API change* + open an issue first |
| Report a security issue | [Security Disclosures](#security-disclosures) — **do not** open a public issue |
| Get commit access | [Becoming a Maintainer](#becoming-a-maintainer) |

---

## Reporting Bugs

Bugs are tracked as [GitHub Issues](https://github.com/mf2023/Encre/issues) (mirrored on [Gitee](https://gitee.com/dunimd/encre/issues)). Before opening a new one:

1. Search the existing issues (open **and** closed) for the same symptom.
2. Read [README.md](../README.md) and the relevant section of this guide — many "bugs" are documented behavior.
3. Confirm the bug is reproducible on a clean clone with the latest `master`.

When you file, include:

- **One-sentence title** that says what is broken, not what you were doing. Good: `EncreAgent.run raises KeyError when tool result is empty`. Bad: `agent broke`.
- **Layer**: Python gateway / Encre Agent Core / Encre Agent Desktop / Documentation. (Pick exactly one — cross-layer bugs usually need two separate issues.)
- **Exact steps to reproduce**, copy-paste runnable. Numbered list, no narrative.
- **Observed vs. expected behavior.** Quote error messages verbatim, including stack traces.
- **Environment**: OS, Python version, Node version, Rust version (`rustc --version`), Encre Agent commit hash, backend provider + model, permission mode.
- **Minimal reproducer**: a script or, for the Encre Agent Desktop, a screen recording.

**Example template:**

```markdown
**Layer:** Python gateway
**Title:** `web_search` returns empty list for queries with non-ASCII characters

**Steps:**
1. `pip install -e ".[all]"`
2. Run the snippet in <https://gist.example/encre-repro-123>
3. Observe: result list is empty
4. Expected: at least 3 hits

**Environment:**
- OS: Ubuntu 24.04
- Python: 3.14.0
- Encre Agent: `0da4040`
- Backend: openai / gpt-4o
- Permission mode: default

**Stack trace:**
Traceback (most recent call last):
  File "...", line 42, in <module>
    ...
```

Issues that don't follow this template may be closed with a request to fill it in. This is not gatekeeping — it is the only way a volunteer maintainer can act on your report within a reasonable time.

---

## Suggesting Enhancements

Enhancements also live as GitHub issues with the `enhancement` label. A good enhancement issue answers four questions:

1. **What user problem does this solve?** (Not "what feature do you want" — *whose life gets better, and how.*)
2. **What does success look like?** Concrete: a CLI flag, a config key, a new tool, an event type.
3. **What is the smallest version that would be useful?** Most features have a 20% version that delivers 80% of the value. Pitch that first.
4. **What alternatives did you consider, and why is this one better?**

Maintainers will respond with one of:

- ✅ **Accepted as proposed** — please open a PR
- 🔁 **Accepted with changes** — we'll discuss the shape in the issue, then you PR
- 📦 **Deferred to a future milestone** — the issue stays open but won't ship soon
- ❌ **Out of scope** — explain why; you are free to fork

Please don't open a PR for an enhancement before the issue has at least one 👍 from a maintainer. We have limited review bandwidth, and unannounced features cause merge conflicts for everyone else.

---

## Pull Request Process

### Branching

- Branch off `master`, never off a release tag.
- Name your branch `type/short-topic`, e.g. `fix/empty-tool-result`, `feat/anthropic-prompt-cache`, `docs/contributing-clarify`.

### Before You Push

Run the checklist below. If any box can't be ticked, the PR will be sent back to you.

- [ ] `ruff check .` is clean (or you have a *very* good reason, written into the PR description).
- [ ] `mypy backend/encre` passes (or the failure is in third-party code you didn't touch).
- [ ] `cargo clippy --all-features -- -D warnings` is clean for Rust changes.
- [ ] `pytest backend/tests -x` passes locally.
- [ ] New behavior has a test. Bug fixes include a *failing* test that the fix turns green.
- [ ] New public API has a docstring.
- [ ] If you changed a user-facing surface (CLI flag, config key, tool signature, REST/WebSocket payload), the relevant section of [README.md](../README.md) is updated in the same PR.
- [ ] If your change closes an issue, you wrote `Closes #NNN` in the PR body.

### Opening the PR

The PR description should contain:

- **What** changed (one paragraph, plain language).
- **Why** (link the issue, or write 2–3 sentences if there's no issue).
- **How to verify** (the commands a reviewer should run).
- **Risk & rollback** (what could break, and how to revert).
- **Screenshots / recordings** for any desktop-app UI change.

A reviewer will be auto-assigned. If none appears in 3 business days, ping a maintainer on the linked issue.

### Review SLA

- **Small fix** (< 30 lines, no API change): first review within **7 days**.
- **Medium change** (new tool / new backend / new adapter): first review within **14 days**.
- **Large change** (API redesign, new architectural layer): we'll discuss timeline in the issue first; please don't open a 1,000-line PR without a heads-up.

Reviewers may request changes. They will not be merged while a review is pending unless the author explicitly marks it ready-for-merge. Stale PRs (no activity for 60 days) may be closed — feel free to reopen when you pick the work back up.

### After Merge

- The PR is squashed into a single date-stamped commit on `master` (see [Commit Messages](#commit-messages)).
- Your contribution is recorded in the next release's [CHANGELOG.md](CHANGELOG.md) under the "Contributors" section.
- Once released, your change ships to every user on the next `pip install --upgrade encre` / desktop auto-update.

---

## Development Setup

### Prerequisites

| Tool | Required version | Why |
|---|---|---|
| Python | 3.14 or newer | `backend/encre/` framework |
| Node.js | 18+ (20+ recommended) | `desktop/` Electron build |
| Rust | 1.65+ (stable) | `native/crates/` native core — only needed if you change Rust code |
| OS | Linux x64/arm64, macOS x64/arm64, Windows x64 | All three are CI-tested |
| Visual Studio Build Tools (Windows only) | 2022, "Desktop development with C++" workload | Required by `node-pty` |

### Clone & Bootstrap

```bash
git clone https://github.com/mf2023/Encre.git encre
cd encre

# One-shot: builds Rust extension, installs Python package, bundles desktop renderer
python build.py
```

If `build.py` is unavailable in your environment (e.g. you are working on documentation only), the manual equivalent is:

```bash
# Python gateway (editable install)
pip install -e .
pip install -e ".[dev]"           # adds pytest, ruff, mypy, pre-commit

# Optional backends / adapters — install only what you need
pip install -e ".[anthropic]"    # Anthropic Claude
pip install -e ".[ollama]"       # Ollama local models
pip install -e ".[discord]"      # Discord adapter
pip install -e ".[slack]"        # Slack adapter
pip install -e ".[telegram]"     # Telegram adapter
pip install -e ".[dingtalk]"     # 钉钉
pip install -e ".[email]"        # Email (IMAP + SMTP)
pip install -e ".[local]"        # Local HF Transformers
pip install -e ".[aws]"          # AWS Bedrock
pip install -e ".[aiohttp]"      # aiohttp-powered HTTP tools
pip install -e ".[native]"       # Pre-built native extension

# Encre Agent Desktop
cd desktop
npm install
cd ..
```

After the install, verify everything is wired up:

```bash
python -c "from encre import EncreAgent; print(EncreAgent)"
encre-doctor                    # if your build installed the CLI shim
```

If `EncreAgent` doesn't import, your `pip install -e .` didn't pick up `backend/encre/` — check that you ran it from the repo root.

---

## Building

### One-shot (recommended)

```bash
python build.py
```

This single command builds the Rust extension, installs the Python package in editable mode, and bundles the Electron renderer. Run it after any change to `pyproject.toml`, `Cargo.toml`, or `desktop/package.json`.

### Layer-by-layer

```bash
# Python wheel only
pip install build
python -m build

# Rust native extension (release)
cd native
cargo build --release -p encre-py
# _native.pyd / _native.so is copied to backend/encre/ automatically on success.

# Encre Agent Desktop
cd desktop
npm run build       # esbuild → dist/main.js, dist/preload.js, renderer/bundle.js
npm start           # build + launch Electron
npm run dist        # build + electron-builder (NSIS / pkg / deb / rpm)
```

The Rust build can take 5–15 minutes from a cold cache. The desktop bundle is incremental (< 30 s on rebuilds).

---

## Running Tests

### Python

`pytest` is configured in `pyproject.toml` with `testpaths = ["backend/tests"]`. Run from the repo root:

```bash
# Everything
pytest -v

# A single module
pytest backend/tests/test_agent.py -v

# A single test by name
pytest backend/tests/test_agent.py -v -k "test_run_streams_text"

# With coverage
pytest --cov=encre --cov-report=term-missing backend/tests/

# Skip slow network-dependent tests
pytest -m "not network" backend/tests/
```

### Rust

```bash
cd native
cargo test --workspace
cargo test -p encre --lib
cargo test -p encre-py
```

### Desktop (renderer type check)

```bash
cd desktop
npm run typecheck
```

The Encre Agent Desktop uses `esbuild` for bundling — there is no separate `npm test` for runtime behaviour unless you add one for your new code.

### CI parity

The reference CI is `.github/workflows/build-binary.yml`. Locally, the closest equivalent is:

```bash
ruff check . && mypy backend/encre && pytest backend/tests -q
cd native && cargo clippy --all-features -- -D warnings && cargo test --workspace
cd ../desktop && npm run typecheck && npm run build
```

If this script passes locally, your PR has an excellent chance of going green on the first CI run.

---

## Code Style

### Python

- **Formatter & linter**: [ruff](https://docs.astral.sh/ruff/). Run `ruff check --fix .` before committing.
- **Type checker**: [mypy](https://mypy-lang.org/) in strict mode for `backend/encre/`. Public functions must have full type annotations.
- **Docstrings**: Google-style for modules and public classes; one-line for trivial helpers.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `SCREAMING_SNAKE_CASE` for module-level constants.
- **Imports**: `from __future__ import annotations` at the top of every module; absolute imports only (`from encre.x import y`).
- **Error handling**: raise specific exception types; never `except Exception:` without re-raising. Agent-facing errors should subclass `EncreError`.

### Rust

- **Formatter**: `cargo fmt` (rustfmt defaults). No exceptions.
- **Linter**: `cargo clippy --all-features -- -D warnings`. Treat warnings as errors.
- **Public items**: must have `///` doc comments; unsafe blocks require a `// SAFETY:` comment.
- **Error type**: prefer `thiserror`-style enums over stringly-typed errors.

### TypeScript / Desktop

- **Formatter**: Prettier defaults (`npm run format` if present in your editor).
- **Type checker**: `npm run typecheck` runs `tsc --noEmit`.
- **React**: function components + hooks. No class components.
- **State**: prefer React's built-in `useState` / `useReducer`. Add a state library only when the existing one (`renderer/src/state.ts`) cannot express the transition.

### Naming for new modules

When you add a new backend, adapter, tool, or skill:

- Backend module: `backend/encre/backends/<provider>.py`, exporting `create_backend(config) -> Backend`.
- Adapter module: `backend/encre/adapters/<platform>.py`, subclassing `BaseAdapter`.
- Tool module: `backend/encre/tools/builtin/<tool>.py`, subclassing `EncreTool`.
- Skill module: `backend/encre/skills/<skill>.py`, registered in `builtin.py`.

In all four cases, register the new module in the corresponding `__init__.py` and add an entry to the catalog test (`backend/tests/test_*.py`).

---

## Commit Messages

Encre Agent uses **date-based commit messages**. Every commit on `master` is a single line of the form `YYYY.MM.DD` representing the date the change was merged.

```
2026.06.21
```

### Rules

- The date is the **merge date**, not the authoring date.
- No prefix (`feat:`, `fix:`, …), no body, no footer.
- The PR number and the detailed changelog live in [CHANGELOG.md](CHANGELOG.md), not in the commit message.

This is unusual, but it has real benefits:

- **Trivial to write.** Maintainers squash-merge with the click of a button; you don't have to think about Conventional Commits, scopes, or breaking-change footers.
- **Chronological order == commit order.** `git log` is a timeline.
- **Detailed change info lives in one place** — the changelog — instead of being scattered across hundreds of commits.

### How it works

- Author your branch normally (`git commit -m "wip"`, etc.).
- Maintainer squashes the PR into a single commit on `master` with the merge date as the message.
- The PR body becomes the next changelog entry. You can draft it in `.github/PULL_REQUEST_TEMPLATE.md` style — the maintainer will edit if needed.

### What this means for you

- You don't need to craft a beautiful commit message. Just write commits that describe your *intent* (`wip`, `address review`, `fix typo`) — they'll be squashed.
- **Do** write a great PR description. That is the artefact reviewers and users actually read.

---

## Project Structure

```
encre/
├── README.md / README.zh.md   # User-facing documentation
├── LICENSE                    # Apache 2.0
├── pyproject.toml             # Python package metadata + deps
├── build.py                   # One-shot build: Rust + Python + Desktop
├── package-lock.json          # Locked node deps (kept in sync with desktop/)
│
├── backend/                   # Python Agent framework
│   ├── encre/                 # The `encre` package
│   │   ├── agent.py           # EncreAgent — public Agent class
│   │   ├── loop.py            # EncreLoop — execution loop
│   │   ├── session.py         # EncreSession — conversation state
│   │   ├── safety.py          # EncreSafetyEngine — 6 permission modes
│   │   ├── autosafety.py      # EncreAutoSafetyClassifier
│   │   ├── config.py          # Configuration management
│   │   ├── backend.py         # create_backend() factory
│   │   ├── backends/          # 31 LLM provider adapters
│   │   ├── adapters/          # 18 chat-platform adapters
│   │   ├── agents/            # Built-in sub-agent configs (9 roles)
│   │   ├── channels/          # WebSocket / terminal / HTTP / slash transports
│   │   ├── tools/             # 36 built-in tools + registry + MCP
│   │   │   └── builtin/       # Individual tool implementations
│   │   ├── hooks/             # EncreHookSystem
│   │   ├── memdir/            # Persistent memory (frontmatter Markdown)
│   │   ├── skills/            # Skill registry + 11 built-in skills
│   │   ├── swarm/             # Teammate / mailbox / consensus
│   │   ├── task/              # Task manager + bash/agent/workflow executors
│   │   ├── server/            # WebSocket server + admin HTTP API
│   │   ├── gateway/           # Gateway client/server protocol
│   │   ├── compact/           # Context compaction (9 strategies)
│   │   ├── lsp/               # LSP client + multi-language server discovery
│   │   ├── computer/          # Desktop & browser automation
│   │   ├── evolution/         # Meta-cognition, reflex, strategy optimizer
│   │   ├── feedback/          # Jaccard-based error-correction learner
│   │   ├── notebook/          # Interactive IPython kernel
│   │   ├── plugins/           # Plugin registry + manifest types
│   │   ├── profile/           # Persona inference
│   │   ├── soul/              # Soul-system files (persona / memory)
│   │   ├── spec/              # Spec document engine
│   │   ├── prompts/           # Prompt chunks, skills, safety, goals
│   │   ├── sandbox/           # Docker container sandbox
│   │   ├── search/            # MCP-based web search
│   │   ├── learning/          # Skill generation & consolidation
│   │   ├── rules/             # Rules loader
│   │   ├── thinking/          # Thinking-config parser
│   │   ├── iclaw/             # iClaw CLI runner
│   │   ├── git/               # Git repo + diff utilities
│   │   └── utils/             # IDs, token counting, type helpers
│   └── tests/                 # pytest suite (configured by pyproject.toml)
│
├── native/                    # Rust workspace
│   ├── Cargo.toml             # Workspace root
│   └── crates/
│       ├── encre-core/        # Native core (crate name: `encre`)
│       │   └── src/
│       │       ├── lib.rs
│       │       ├── fs.rs          # Native read / write
│       │       ├── search.rs      # Regex search, glob
│       │       ├── simd_search.rs # SIMD-accelerated pattern matching
│       │       ├── diff.rs        # Unified diff + apply
│       │       ├── shell.rs       # Sandboxed shell execution
│       │       ├── sandbox.rs     # Sandbox result types
│       │       ├── landlock.rs    # Linux Landlock enforcement
│       │       ├── tokenizer.rs   # Heuristic token counter
│       │       ├── embedding.rs   # Cosine / Jaccard similarity
│       │       ├── lsp_proto.rs   # LSP JSON-RPC parser
│       │       └── permission.rs  # Native permission checks
│       └── encre-py/          # PyO3 bindings → `encre._native`
│           └── src/lib.rs
│
├── desktop/                   # Electron Encre Agent Desktop
│   ├── main.ts                # Electron main process
│   ├── preload.ts             # Context bridge (IPC)
│   ├── build.js               # esbuild config
│   ├── package.json           # Node deps + scripts
│   ├── electron-builder.yml   # NSIS / pkg / deb packaging
│   ├── tsconfig.json          # Main-process TypeScript config
│   ├── fetch_icons.js         # Icon-fetch script
│   └── renderer/              # Frontend (React 19)
│       ├── index.html
│       ├── src/               # TypeScript source
│       │   ├── locales/       # en.ts / zh.ts
│       │   └── …
│       └── vs/                # Monaco Editor (pre-built)
│
├── docs/                      # Project documentation (you are here)
│   ├── CONTRIBUTING.md        # This file
│   ├── CODE_OF_CONDUCT.md
│   ├── SECURITY.md
│   ├── CHANGELOG.md
│   ├── legal/                  # Jurisdiction-specific legal documents
│   │   ├── README.md           # Legal documents index
│   │   ├── asia/               # cn · jp · kr · sg · tw · in · ae · sa · il
│   │   ├── europe/             # eu · uk · ch · tr
│   │   ├── americas/           # us · ca · br · mx
│   │   ├── africa/             # za · ng · ke · eg
│   │   └── oceania/            # au · nz
│   ├── PLAN.md
│   └── THANKS.md / THANKS_CN.md
│
└── .github/
    └── workflows/
        └── build-binary.yml   # CI: build release binaries
```

### Where to put new code

| You're adding … | Put it in | Register in |
|---|---|---|
| A new LLM backend | `backend/encre/backends/<provider>.py` | `backends/__init__.py`, `backends/catalog.py` |
| A new chat-platform adapter | `backend/encre/adapters/<platform>.py` | `adapters/__init__.py`, `adapters/manager.py` |
| A new built-in tool | `backend/encre/tools/builtin/<tool>.py` | `tools/builtin/__init__.py`, `tools/registry.py` |
| A new sub-agent role | `backend/encre/agents/builtin.py` (entry) | `agents/__init__.py` |
| A new built-in skill | `backend/encre/skills/<skill>.py` | `skills/builtin.py` |
| A new Rust module | `native/crates/encre-core/src/<area>.rs` | `lib.rs` re-export list |
| A new desktop panel / view | `desktop/renderer/src/<area>.ts` | `desktop/renderer/src/app.ts` view registry |
| A new locale string | `desktop/renderer/src/locales/<locale>.ts` | (auto-picked up by `i18n.ts`) |

If your contribution spans more than one of these layers — typical for "add a feature end-to-end" — open **one PR per layer**. Layered PRs are easier to review and to revert.

---

## Release & Versioning

Encre Agent follows semantic versioning:

- **Major** (X.0.0): public-API breaking changes. Posted to the issue tracker at least 30 days in advance.
- **Minor** (0.X.0): new features, new backends, new tools, additive only.
- **Patch** (0.0.X): bug fixes, docs, performance. No API change.

Releases are cut by a maintainer from `master` once a week if there are merged changes. The release process is:

1. Bump version in `pyproject.toml` and `desktop/package.json` (kept in lockstep).
2. Aggregate the merged PRs into [CHANGELOG.md](CHANGELOG.md) with author credits.
3. Tag `vX.Y.Z`, push, GitHub Actions builds wheels + installers.
4. Announce in [THANKS.md](THANKS.md) and on the discussion channels.

---

## Becoming a Maintainer

We promote contributors to maintainers when all three are true:

1. They have **at least 5 merged PRs** of non-trivial size (excluding typos, docs-only, and CI-only changes).
2. They have **participated in code review** for at least 3 PRs from other contributors (comments, suggestions, approvals).
3. They have been **responsive** in issues and PRs on the topics they want to maintain (e.g. the Encre Agent Desktop, a specific backend family).

Promotion is by maintainer consensus and is announced in [CHANGELOG.md](CHANGELOG.md). Maintainers retain the right to push to `master`, tag releases, and triage issues. There is no formal demotion process; inactive maintainers are moved to emeritus status after 6 months of inactivity, and can rejoin on request.

---

## Communication Channels

- **GitHub Issues** (English, primary): https://github.com/mf2023/Encre/issues — bug reports, enhancement proposals
- **GitHub Discussions** (English): general questions, "how do I…", showcase projects
- **Gitee Issues** (中文镜像): https://gitee.com/dunimd/encre/issues — bug reports, enhancement proposals
- **X / Twitter**: [@Dunimd2025](https://x.com/Dunimd2025) — release announcements
- **Bilibili**: [@Dunimd](https://space.bilibili.com/3493284091529457) — long-form demos in Chinese
- **Maintainer email** (private, for security or licensing only): see [SECURITY.md](SECURITY.md)

When a conversation moves between channels, link the two threads so future readers can follow the trail.

---

## Security Disclosures

**Do not file security issues as public GitHub issues.** Email the maintainers directly (address in [SECURITY.md](SECURITY.md)) with:

- A description of the vulnerability and its impact.
- A reproducer (proof-of-concept script, screenshot, or curl invocation).
- Whether you intend to disclose publicly, and on what timeline.

We commit to:

- Acknowledge your report within **3 business days**.
- Provide a remediation timeline within **10 business days** of confirmation.
- Credit you in the security advisory (unless you ask to remain anonymous).
- Coordinate disclosure so users have a fix before the issue becomes public.

---

## License

By contributing to Encre Agent, you agree that your contributions will be licensed under the [Apache License 2.0](../LICENSE). The full text is in [`LICENSE`](../LICENSE). A short summary:

- You can use, modify, and distribute Encre Agent, including commercially.
- You must include the license and copyright notice.
- You must state any changes you make.
- You cannot use contributor names to endorse derived works without permission.
- There is **no warranty**. See `LICENSE` §7 and §8.

If your contribution is based on someone else's code, make sure that code is compatible with Apache 2.0 (BSD-2-Clause, BSD-3-Clause, MIT, MPL-2.0, ISC, and similar are all fine; GPL-family licenses are **not**) and that the original attribution is preserved in your PR.

---

Thank you for reading this far — and for making Encre Agent better. Every PR, issue, doc fix, and review comment helps.
