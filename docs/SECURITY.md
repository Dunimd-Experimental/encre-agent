# Security Policy

The Encre Agent project takes security seriously — both the security of users running the Agent on their machines, and the security of contributors working on the codebase. This document covers:

1. **Supported versions** — which versions get security fixes
2. **How to report a vulnerability** — and what to expect
3. **Built-in security architecture** — the layers already protecting you
4. **Hardening checklist** — what *you* should configure before running in production
5. **Threat model** — what Encre Agent defends against and what it does not
6. **For contributors** — how to write code that doesn't introduce security regressions

If you find a vulnerability, **do not file a public issue**. Email the maintainers at the address in [Reporting a Vulnerability](#reporting-a-vulnerability).

---

## Supported Versions

The following table lists which Encre Agent release lines currently receive security updates. Encre Agent uses [semantic versioning](https://semver.org/); the current version is reflected in [`pyproject.toml`](../pyproject.toml) (Python) and [`desktop/package.json`](../desktop/package.json) (Electron). The two are kept in lockstep.

| Release line | Status | Security fixes | End-of-life |
|---|---|---|---|
| `0.5.x` (current pre-release) | 🟡 Pre-release | Yes — best-effort, fast turnaround | Until `0.6.0` ships |
| `0.4.x` | ❌ End-of-life | No | Please upgrade |
| `< 0.5.0` | ❌ End-of-life | No | Please upgrade |

Users are encouraged to track `master` or the latest tagged release. Because Encre Agent is distributed primarily as a single repo (cloned and `pip install -e .`-ed) rather than as a PyPI package, "upgrade" means:

```bash
cd encre
git pull
pip install -e .
```

For the Encre Agent Desktop, follow the auto-update prompt or reinstall from the latest GitHub / Gitee release artifact.

---

## Reporting a Vulnerability

### Contact

🌐 **security.dunimd.com** *(pending activation)* — monitored by at least two maintainers

🔑 **GPG:** `54B6 1520 0435 612E E91C F180 24AD F10D D207 AA1F` ([verify](https://keys.openpgp.org/search?q=24ADF10DD207AA1F))

```bash
gpg --keyserver hkps://keys.openpgp.org --recv-keys 24ADF10DD207AA1F
gpg --output report.gpg --encrypt --sign --recipient dunimd@outlook.com report.txt
```

For **non-sensitive** security questions (e.g. "is this configuration safe?"), you may also open a [GitHub Discussion](https://github.com/mf2023/Encre/discussions) tagged `security-question`. When in doubt, prefer email — disclosure timing matters.

For **Code of Conduct violations**, use `conduct.dunimd.com` *(pending activation)* instead. The two addresses are read by overlapping but distinct maintainer groups.

### What to Include

A useful report contains:

- **Description** — what the vulnerability is, in one paragraph
- **Impact** — what an attacker can achieve (RCE, data exfiltration, sandbox escape, prompt-injection-mediated action, …)
- **Affected versions** — which Encre Agent release lines are vulnerable
- **Affected layer** — Python gateway / Encre Agent Core / Encre Agent Desktop / Documentation
- **Steps to reproduce** — numbered, copy-paste runnable
- **Proof of concept** — script, payload, screenshot, or recording. Especially valuable for: tool injection, prompt injection, sandbox escape, SSRF bypass, credential leakage
- **Suggested fix** — if you have one (a patch is a bonus, not a requirement)
- **Your contact info** — for follow-up questions (optional but recommended)
- **Disclosure preference** — do you want to be credited in the advisory? Coordinated disclosure timeline you'd prefer?

**Template:**

```markdown
**Title:** `<one-line summary>`

**Layer:** Python / Rust / Desktop / Docs
**Affected versions:** 0.5.0 (commit <hash>)
**Severity (your estimate):** Critical / High / Medium / Low

**Description:**
<paragraph>

**Impact:**
<what an attacker achieves>

**Steps to reproduce:**
1. `pip install -e .`
2. `python ...`
3. Observe ...

**Suggested fix:**
<optional>

**Disclosure preference:**
<e.g. "credit me as <name>" / "keep me anonymous" / "I plan to publish on YYYY-MM-DD">
```

### What to Expect

When you submit a security report, our process is:

| Stage | Timeline | What happens |
|---|---|---|
| **Acknowledgment** | within **48 hours** | A maintainer confirms receipt, assigns a tracking ID (e.g. `SEC-2026-014`), and confirms the affected versions. |
| **Initial assessment** | within **5 business days** | We confirm or refute the report, assign a severity (Critical / High / Medium / Low), and decide whether to issue a CVE. |
| **Investigation** | 1–14 days | We dig in, write a reproducer internally, and design a fix. We may reach out for clarification. |
| **Fix development** | depends on severity | See [Response Time SLAs](#response-time-slas). |
| **Disclosure coordination** | before public release | We agree a disclosure date with you (default: when the fix ships). |
| **Public advisory** | at disclosure | GitHub Security Advisory + CHANGELOG entry + CVE record if applicable. |

### Response Time SLAs

| Severity | Initial response | Fix timeline | Examples |
|---|---|---|---|
| **Critical** | 24 hours | 7 days | Remote code execution, sandbox escape, unauthenticated RCE via a chat adapter |
| **High** | 48 hours | 14 days | SSRF bypass, prompt injection leading to data exfiltration, credential leakage to logs |
| **Medium** | 5 business days | 30 days | Privilege escalation between permission modes, DoS via crafted input, info disclosure |
| **Low** | 10 business days | 60 days | Defense-in-depth weaknesses, hardening opportunities, minor info leaks |

These are targets, not promises. We will tell you early if we need more time, and why.

### Our Commitments to You

- We will **acknowledge** your report within 48 hours
- We will **keep you informed** of progress, at minimum at the milestone stages above
- We will **credit you** in the public advisory, unless you ask to remain anonymous
- We will **not take legal action** against researchers who follow this policy in good faith
- We will **coordinate disclosure** so users have a fix before the issue becomes public
- We will **not share your report** outside the maintainer group without your consent, except as required by law

---

## Built-in Security Architecture

Encre Agent ships with several layers of defense, **on by default** unless you turn them off. Understanding what is already in place helps you avoid disabling protections you didn't mean to.

### Layer 1: Six-Level Permission System

`EncreConfig.permission_mode` controls how aggressively the Agent acts on its own:

| Mode | Behavior | Use when |
|---|---|---|
| `bypass` | No checks. The Agent does whatever its tools allow. | Local experiments where you trust the prompt. **Never** for shared systems. |
| `dont_ask` | Auto-allow all tool calls. | Sandboxed CI / batch jobs. |
| `accept_edits` | Auto-allow file edits, ask for other actions. | Heavy code work where you want to skim diffs but not be prompted for each `cat`. |
| `plan` | Require approval for all disruptive operations. | Research / exploration of unfamiliar code. |
| `auto` | Heuristic decisions via `EncreAutoSafetyClassifier`. | Power users who want speed with sensible defaults. |
| `default` | Always ask for user confirmation before executing tools. | **Recommended** for first use and for any prompt you didn't write yourself. |

The default is configurable per-tool via `EncreConfig.permission_settings` (e.g. `{"bash": "ask", "file_write": "allow", "network": "deny"}`).

### Layer 2: Dangerous-Command Detection

The Bash tool's command text is checked against the blacklist in
[`dangerous_commands.txt`](../harness/encre/dangerous_commands.txt) inside the Rust
permission engine (`permission_check_mode`), which is what actually denies or
prompts a call. The table is sectioned by shell family (`[universal]`, `[linux]`,
`[windows]`, `[macos]`): the host's own section always applies and the section
matching the shell named in the tool's `terminal` argument applies too, because
that argument is model-supplied. `encre.safety.analyze_bash_command` is a separate
static analyzer used for the network-access flag and for scoring in `auto`
permission mode -- it is not the enforcement point. Patterns detected by default
include `rm -rf /`, raw `dd` against devices, fork-bombs, reverse shells, remote
script piped into an interpreter, and known destructive package commands. You can
extend the list via `EncreConfig.dangerous_command_patterns`; extra entries are
appended as `[universal]` rules, so they match under every shell.


### Layer 3: Sandboxed Execution

Two complementary mechanisms:

- **Docker container sandbox** ([`encre/sandbox/container.py`](../backend/encre/sandbox/container.py)) — runs shell tools inside a container with seccomp-style syscall filtering and configurable memory/CPU limits. Enable with `EncreConfig(sandbox_enabled=True)`.
- **Linux Landlock** ([`native/crates/encre-core/src/landlock.rs`](../native/crates/encre-core/src/landlock.rs)) — kernel-level filesystem and network restrictions on Linux. No Docker required. Used by `shell.rs` when available.

Both are layered: even if one is bypassed, the other still applies.

### Layer 4: SSRF Protection

`EncreSSRFGuard` ([`encre/ssrf.py`](../backend/encre/ssrf.py)) validates URLs before any HTTP fetch:

- Resolves the hostname via DNS
- Compares the resolved IP against a CIDR blocklist of private, loopback, link-local, multicast, and reserved ranges
- Blocks redirects to blocked ranges
- Applied to `web_fetch`, `web_search`, MCP HTTP transports, and adapter callbacks

### Layer 5: Encrypted Credential Storage

`encre/crypto.py` provides AES-256-GCM helpers. The Encre Agent Desktop uses them to encrypt browser cookies at rest; the server stores API keys encrypted at rest when configured to. Key files live in the user's data directory (`get_data_dir()`) and are never written to logs.

### Layer 6: Multi-Agent Isolation

When multiple Agents run in a Swarm, each gets its own session, memory namespace, and tool-policy context (`current_tool_policy` in `EncreConfig`). Cross-agent writes go through the `EncreBlackboard` API, which is logged.

### Layer 7: Hook System for Custom Policy

You can attach a handler to any hook event (e.g. `pre_tool_exec`) and have it block, modify, or log the call. This is the right place to enforce organization-specific policy without forking Encre Agent.

```python
from encre.config import EncreConfig, EncreAgent
from encre.hooks.system import EncreHookSystem
from encre.hooks.types import HookEventType

config = EncreConfig(backend_type="openai", api_key="...", permission_mode="default")
hooks = EncreHookSystem()

def block_writes_outside_workspace(event):
    target = event.tool_input.get("path", "")
    if not target.startswith(config.workspace):
        return {"block": True, "reason": f"writes outside {config.workspace} are blocked"}
    return None

hooks.register_handler(
    HookEventType.PRE_TOOL_EXEC,
    block_writes_outside_workspace,
    handler_id="workspace-guard",
    metadata={"source": "user-config"},
)

agent = EncreAgent(config=config, hook_system=hooks)
```

See [`encre/hooks/`](../backend/encre/hooks/) for the full event catalog.

---

## Hardening Checklist for Production Use

Before you point Encre Agent at anything that isn't your own laptop, run through this list.

### Pre-deployment

- [ ] **Pin a version.** Use a specific tag, not `master`.
- [ ] **Set `permission_mode="default"`** for any prompt you didn't author yourself.
- [ ] **Enable sandboxing:** `EncreConfig(sandbox_enabled=True)` plus, on Linux, Landlock.
- [ ] **Lock `permission_settings`** with explicit per-tool overrides, especially `bash`, `file_write`, and any tool that touches the network.
- [ ] **Set API keys via environment variables**, never in config files committed to the repo.
- [ ] **Encrypt the config file** if you must store API keys in it; do not log `EncreConfig` instances at `DEBUG` level.
- [ ] **Review the workspace path.** Encre Agent will, by default, only operate on the configured `workspace` directory. Verify this is what you intend.
- [ ] **Audit the tool registry.** Run `agent.tool_registry.list_tools()` and confirm you recognize every tool that is enabled.
- [ ] **Disable telemetry** if your compliance posture forbids outbound calls: `EncreConfig(telemetry_enabled=False)`.
- [ ] **Disable tracing** unless you intentionally want OpenTelemetry export: `EncreConfig(tracing_enabled=False)`.

### Runtime

- [ ] Run as a **non-root user**. The Encre Agent Desktop and the Agent process should never have root.
- [ ] Put the host behind a **firewall** that blocks outbound traffic to LLM providers you don't use.
- [ ] **Log at `INFO`** (not `DEBUG`/`TRACE`) — debug logs include full prompts and tool inputs.
- [ ] **Monitor** `pre_tool_exec` and `tool_exec_failed` events for unusual patterns.
- [ ] **Rotate API keys** on the same cadence as any other credential in your stack.
- [ ] **Update dependencies** regularly:
  ```bash
  # Python: pin and review
  pip install --upgrade pip
  pip-audit                       # if you have it installed

  # Rust: native extension
  cd native
  cargo install --locked cargo-audit
  cargo audit

  # Desktop
  cd desktop
  npm audit
  npm audit fix
  ```

### For multi-agent / server deployments

- [ ] Isolate each Agent's `workspace` directory on its own filesystem mount (or container).
- [ ] Front the WebSocket server (`encre.channels.websocket.WebSocketServer`) with TLS and an authentication layer — Encre Agent does not implement auth by default.
- [ ] Bind admin HTTP APIs (`encre.channels.http_api`) to `127.0.0.1` unless you have a reverse proxy with auth in front.
- [ ] For Swarm setups, ensure the `EncreMailbox` and `EncreBlackboard` storage is on encrypted, access-controlled storage.

---

## Threat Model

Encre Agent is designed for **single-user, locally-trusted execution**, optionally extended with chat-platform integration. The threat model below tells you where the line is.

### In scope (we defend against)

- **Accidental damage** by a competent user running a model they wrote a prompt for — typos in `rm -rf`, accidental writes outside the workspace, etc.
- **Tool-injection in retrieved content** — a web page or document contains a prompt-injection payload that tries to make the Agent call a tool with attacker-controlled args. Mitigated by permission modes + output validation + SSRF guard.
- **SSRF via `web_fetch` / `web_search`** — attacker tricks the Agent into hitting an internal endpoint. Mitigated by the SSRF guard.
- **Credential leakage to logs** — Mitigated by the logging redaction in `logging_config.py` and the recommendation to log at `INFO`.
- **Sandbox escape from the Docker container** — Mitigated by seccomp + Landlock + non-root user.
- **Cross-agent data leakage in Swarm** — Mitigated by per-agent session/memory namespaces.

### Out of scope (we do **not** defend against)

- **A user who runs Encre Agent as root and approves every tool call** — they have explicitly opted out of protection.
- **A user who puts their API key in a public GitHub repo** — Encre Agent cannot undo social engineering.
- **A user who turns off all permission modes and gives the Agent a hostile prompt** — you have configured the system to be unsafe; the threat model does not apply.
- **Supply-chain attacks on transitive dependencies** — we monitor via `cargo audit` and `npm audit` and patch promptly, but we cannot guarantee every dependency is clean at every moment.
- **LLM-provider compromise or prompt-logging by the provider** — outside our trust boundary. Choose providers whose data-handling policy you have read.
- **A determined attacker with local code execution on the same machine** — once they can run code as your user, Encre Agent's protections are bypassed; protect the host.

If you need guarantees beyond this threat model, run Encre Agent inside a hardened VM or air-gapped environment — and remember that even there, an LLM with tool access is only as trustworthy as the prompt and the audit trail.

---

## For Contributors: Writing Code That Doesn't Introduce Security Regressions

If you're contributing to Encre Agent itself, this section is for you.

### Don't

- **Don't add a tool** that reads arbitrary URLs without going through `EncreSSRFGuard`. The pattern is:
  ```python
  from encre.ssrf import EncreSSRFGuard
  guard = EncreSSRFGuard()
  if not guard.validate(url):
      raise PermissionError(f"URL blocked by SSRF policy: {url}")
  ```
- **Don't log tool inputs or outputs at `DEBUG`/`TRACE` without explicit redaction**. Tool inputs frequently contain credentials, paths to sensitive files, and full prompt text.
- **Don't accept new dependencies** under GPL-family licenses — Apache 2.0 is incompatible. The accepted list lives in [CONTRIBUTING.md → License](../CONTRIBUTING.md#license).
- **Don't disable security checks** to make a test pass. Add a test that exercises the check and call it from the test.
- **Don't shell out via `subprocess.run(shell=True)`** with user-controlled input. Use `shell=False` and a list of arguments.
- **Don't extend the dangerous-command list** to *allow* something by default — additions should only ever make the list stricter.

### Do

- **Do add tests** for any new code path that handles untrusted input. The threat model above is the spec; the test is the proof.
- **Do run `pip-audit` and `cargo audit`** before opening a PR that touches dependencies.
- **Do add a CHANGELOG entry** under a "Security" heading when your change is security-relevant.
- **Do mention security implications** in the PR description, even if your change isn't primarily security work. Reviewers will appreciate it.
- **Do add a hook event** for new kinds of tool execution, so users can write policy handlers.
- **Do fuzz new parsers** before merging. There are example fuzz harnesses in [`backend/tests/fuzz/`](../backend/tests/fuzz/) — extend rather than fork.

### Reviewer checklist (security section)

When reviewing a PR, explicitly check:

- [ ] No new dependencies with restrictive licenses (GPL, AGPL, LGPL, SSPL, BUSL)
- [ ] No new subprocess calls with `shell=True` and user input
- [ ] No new network calls bypassing `EncreSSRFGuard`
- [ ] No new logging at `DEBUG`/`TRACE` of full prompts or tool inputs
- [ ] New tools have permission-mode handling that respects `EncreConfig.permission_mode`
- [ ] Dangerous-command patterns extended in the *restrictive* direction
- [ ] If the PR changes a public API, [CONTRIBUTING.md → API change](../CONTRIBUTING.md) review process has been followed

---

## Known Limitations

We document these so you can make informed decisions about where to deploy Encre Agent. None of these is a bug; they are design constraints of the architecture.

1. **LLM outputs are non-deterministic.** Even with the same prompt and seed, a model may produce different tool calls. Do not assume idempotency for destructive operations.
2. **Prompt injection is an unsolved problem.** `EncreAutoSafetyClassifier` mitigates but cannot eliminate it. Treat retrieved content as untrusted; validate tool inputs at the boundary.
3. **Tool outputs leave the local machine.** When you use a cloud LLM, tool results are sent to the provider. Choose providers whose data-handling policy you have read, or run a local model via the `ollama` / `local` backends.
4. **The Encre Agent Desktop uses Chromium** (via Electron). It inherits Chromium's CVE cadence. Run `npm audit` regularly.
5. **Landlock is Linux-only.** On macOS and Windows, the Docker sandbox is the primary isolation mechanism.

---

## Security Resources

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/)
- [Rust Security Guidelines](https://doc.rust-lang.org/nomicon/)
- [cargo-audit](https://github.com/RustSec/cargo-audit)
- [Python Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Python_Security_Cheat_Sheet.html)
- [Electron Security Checklist](https://www.electronjs.org/docs/latest/tutorial/security)

## Acknowledgments

We thank the following researchers who have responsibly disclosed vulnerabilities. (This list is updated as advisories are published.)

*— no public advisories yet —*

---

**Last Updated:** 2026-08-23
**Version:** 1.1
