# EA Tool Packages

Bundled EA (Encre Agent) extension packages. **The directory itself is the
registry** — discovery is fully automatic and requires no registration code
anywhere.

## Packages

| Package | Tier | Tools |
|---------|------|-------|
| `ea-tool-agent` | system-default | `agent` |
| `ea-tool-apply-patch` | system-default |  |
| `ea-tool-archive` | system-default |  |
| `ea-tool-browser` | system-default | `browser` |
| `ea-tool-chart` | system-default | `chart` |
| `ea-tool-cloud-storage` | system-default | `cloud_storage` |
| `ea-tool-computer-use` | system-default | `computer` |
| `ea-tool-cron` | system-default | `cron` (create, list, delete) |
| `ea-tool-database` | system-default | `database` |
| `ea-tool-deploy` | system-default | `deploy` |
| `ea-tool-device` | system-default | `device` (info, battery, location, network, sensor) |
| `ea-tool-diagram` | system-default | `diagram` |
| `ea-tool-diff` | system-default |  |
| `ea-tool-docker` | system-default | `docker` |
| `ea-tool-document` | system-default | `document` |
| `ea-tool-email` | system-default | `email` |
| `ea-tool-env-manager` | system-default | `env_manager` |
| `ea-tool-generate-image` | system-default | `edit_image`, `generate_image`, `image_variation` |
| `ea-tool-git-tool` | system-default | `git` |
| `ea-tool-github` | system-default | `github` |
| `ea-tool-glob` | system-default | `glob` |
| `ea-tool-grep` | system-default | `grep` |
| `ea-tool-hash-crypto` | system-default | `hash_crypto` |
| `ea-tool-image` | system-default | `image` |
| `ea-tool-info` | system-default | `info` |
| `ea-tool-json-tool` | system-default | `json_tool` |
| `ea-tool-lint-format` | system-default | `lint_format` |
| `ea-tool-lsp` | system-default | `lsp` |
| `ea-tool-media` | system-default | `media` |
| `ea-tool-media-api` | system-default | `create_embeddings`, `create_moderation`, `transcribe_audio`, `translate_audio` |
| `ea-tool-notebook` | system-default | `notebook` |
| `ea-tool-notify` | system-default | `notify` |
| `ea-tool-pdf` | system-default | `pdf` |
| `ea-tool-platform-api` | system-default | `batch_api`, `file_api`, `fine_tuning_api` |
| `ea-tool-presentation` | system-default | `presentation` |
| `ea-tool-qr-code` | system-default | `qr_code` |
| `ea-tool-question` | system-default | `question` |
| `ea-tool-rest-client` | system-default | `rest_client` |
| `ea-tool-skill` | system-default | `skill` |
| `ea-tool-spreadsheet` | system-default | `spreadsheet` |
| `ea-tool-ssh` | system-default | `ssh` |
| `ea-tool-task` | system-default | `task` (create, list, get, output, stop, update) |
| `ea-tool-test-runner` | system-default | `test_run` |
| `ea-tool-todo` | system-default | `todo` |
| `ea-tool-translation` | system-default | `translation` |
| `ea-tool-web-fetch` | system-default | `web_fetch` |
| `ea-tool-workflow` | system-default | `workflow` |

## Skill Packages

Non-tool skills are packaged one-per-skill as `ea-skill-<name>`:

| Package | Tier | Skill |
|---------|------|-------|
| `ea-skill-<name>` | system-default | `<name>` |

The 185 bundled skills live under `ea-skill-*/<module>/skills/<name>/SKILL.md`
(a verbatim copy of the legacy `encre/skills/builtin/<name>/` folder, including
`references/`, `assets/` and scripts).  They are loaded via
`EncreSkillRegistry.load_from_dir` and, like tool packages, are discovered by
the directory scan and shipped automatically inside the PyInstaller bundle.
The package set was fully audited: 34 duplicate / hollow / placeholder packages
were removed and their unique guidance merged into the surviving primary
packages (see merge appendix sections inside those `SKILL.md` files).

## Tier Classification

| Tier | Value | Behavior |
|------|-------|----------|
| Built-in mandatory | `mandatory` | Hardcoded in core, cannot be disabled |
| System default | `system-default` | Ships with package, can be uninstalled |
| User custom | `user` | External pip installation |

Tier is declared per package in `pyproject.toml`:

```toml
[tool.ea]
tier = "mandatory"
```

- `mandatory` / `system-default`: auto-discovered by the directory scan and
  auto-activated. `mandatory` additionally cannot be deactivated.
- `user`: third-party packages installed via pip; discovered through the
  `ea.plugins` entry-point group instead of this directory.

## Discovery (zero registration)

The EA directory scanner (`encre.plugins.ea_scan`) scans every `ea-tool-*` and
`ea-skill-*` folder under `ea_tools/`, reads its `pyproject.toml`, inserts the package on
`sys.path`, imports the plugin factory, and registers + auto-activates it.
The same logic runs in frozen (PyInstaller) builds, where the whole `ea_tools/`
tree is shipped under `sys._MEIPASS/ea_tools/`.

**Drop a new package folder into this directory and it just works** — both at
runtime and in the next PyInstaller build (the whole tree is staged and packed
as data).

Skills are loaded automatically too: each `<module>/skills/*.md` is picked up
by `EncreSkillRegistry.load_from_dir` at agent startup (BUNDLED priority).

## Structure

```
core/ea_tools/
  ea-tool-web-fetch/
    README.md
    pyproject.toml          # Metadata + [tool.ea] tier
    ea_tool_web_fetch/
      __init__.py           # Lazy re-exports
      tool.py               # Tool implementation (build_tool)
      plugin.py             # EncrePlugin manifest + create_plugin factory
      skills/
        tool-web_fetch.md   # Skill instructions (YAML frontmatter)
```

## Standalone install (optional)

Each package remains a valid pip project, so it can also be built and
installed standalone:

```bash
pip install ./ea-tool-web-fetch
```

The `ea.plugins` entry point in each package keeps that path working; inside
the Encre app the directory scan wins (first registered wins).
