---
name: git-commit
description: Execute standardized Git commits with intelligent staging, automatic Conventional Commits message generation, and custom override for type, scope, and description.
---

# Git Commit Skill

Create standardized, semantic git commits using the **Conventional Commits** specification. Analyze the actual diff to determine appropriate type, scope, and message.

## Conventional Commit Format

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

### Commit Types

| Type       | Purpose                  |
|------------|--------------------------|
| feat       | New feature              |
| fix        | Bug fix                  |
| docs       | Documentation only       |
| style      | Formatting (no logic)    |
| refactor   | Code refactor            |
| perf       | Performance improvement  |
| test       | Add/update tests         |
| build      | Build system/dependencies|
| ci         | CI/config changes        |
| chore      | Maintenance/misc         |
| revert     | Revert commit            |

### Breaking Changes

```
feat!: remove deprecated endpoint

feat: allow config to extend configs

BREAKING CHANGE: `extends` key behavior changed
```

## Workflow

### 1. Analyze Diff

```shell
# If files are staged
git diff --staged

# If nothing staged
git diff

# Also check status
git status --porcelain
```

### 2. Stage Files (if needed)

```shell
git add path/to/file1 path/to/file2
git add *.test.*
git add -p   # interactive staging
```

Never commit secrets (.env, credentials.json, private keys).

### 3. Generate Commit Message

Analyze the diff to determine:
- **Type**: What kind of change?
- **Scope**: What area/module is affected?
- **Description**: One-line summary (present tense, imperative mood, <72 chars)

### 4. Execute Commit

```shell
# Single line
git commit -m "<type>[scope]: <description>"

# Multi-line with body/footer
git commit -m "$(cat <<'EOF'
<type>[scope]: <description>

<optional body>

<optional footer>
EOF
)"
```

## Best Practices

- One logical change per commit
- Present tense: "add" not "added"
- Imperative mood: "fix bug" not "fixes bug"
- Reference issues: Closes #123, Refs #456
- Keep description under 72 characters

## Git Safety Protocol

- NEVER update git config
- NEVER run destructive commands (--force, hard reset) without explicit request
- NEVER skip hooks (--no-verify) unless user asks
- NEVER force push to main/master
- If commit fails due to hooks, fix and create NEW commit (don't amend)
