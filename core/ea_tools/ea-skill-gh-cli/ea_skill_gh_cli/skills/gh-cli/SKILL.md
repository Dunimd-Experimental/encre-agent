---
name: gh-cli
description: GitHub CLI (gh) comprehensive reference guide - repositories, issues, pull requests, Actions, projects, releases and more command-line operations for seamless GitHub integration.
---

# GitHub CLI (gh) Reference

Prerequisites: Install `gh` via package manager (`brew install gh`, `winget install --id GitHub.cli`, etc.)

## Authentication

```shell
# Interactive login
gh auth login

# Login with token
gh auth login --with-token < token.txt

# Check status
gh auth status

# Switch accounts
gh auth switch --hostname github.com --user username

# Setup git credential helper
gh auth setup-git

# View token
gh auth token
```

## Configuration

```shell
gh config list                          # List all config
gh config set editor vim                # Set editor
gh config set git_protocol ssh          # Use SSH
gh config set prompt disabled           # Disable prompts
```

Key env vars: `GH_TOKEN`, `GH_HOST`, `GH_REPO`, `GH_PROMPT_DISABLED`, `GH_EDITOR`, `GH_TIMEOUT`

## Repositories

```shell
# Create
gh repo create my-repo --public --description "desc" --license mit --gitignore python

# Clone
gh repo clone owner/repo

# List
gh repo list owner --limit 50 --json name,visibility

# View
gh repo view owner/repo --json name,description,defaultBranchRef

# Edit
gh repo edit --description "New" --homepage https://example.com
gh repo edit --visibility private
gh repo rename new-name

# Fork/Sync
gh repo fork owner/repo --clone --org org-name
gh repo sync --branch main --force

# Delete
gh repo delete owner/repo --yes

# Set default
gh repo set-default owner/repo
```

## Issues

```shell
# Create
gh issue create --title "Bug" --body "desc" --labels bug --assignee @me

# List
gh issue list --state all --limit 50 --assignee @me --labels bug --search "is:open"

# View
gh issue view 123 --comments --json title,body,state,labels

# Edit
gh issue edit 123 --title "New" --add-label bug --remove-label stale
gh issue edit 123 --add-assignee user1 --milestone "v1.0"

# Close/Reopen
gh issue close 123 --comment "Fixed"
gh issue reopen 123

# Comment
gh issue comment 123 --body "Looks good!"
gh issue comment 123 --edit 456789 --body "Updated"

# Other
gh issue pin 123 | gh issue unpin 123
gh issue lock 123 --reason off-topic | gh issue unlock 123
gh issue transfer 123 --repo owner/new-repo
gh issue delete 123 --yes
gh issue develop 123 --branch fix/issue-123

# Status
gh issue status
```

## Pull Requests

```shell
# Create
gh pr create --title "Feature" --body "desc" --base main --head branch \
  --draft --assignee @me --reviewer user1 --labels enhancement

# List
gh pr list --state merged --base main --author @me --labels bug --limit 50

# View
gh pr view 123 --comments --json title,body,state,commits,files

# Checkout
gh pr checkout 123 --branch feat-123

# Diff
gh pr diff 123 --name-only

# Merge
gh pr merge 123 --squash --delete-branch
gh pr merge 123 --merge | --rebase

# Close/Reopen
gh pr close 123 --comment "Closing"
gh pr reopen 123

# Edit
gh pr edit 123 --title "New" --add-label bug --add-reviewer user1 --ready

# Review
gh pr review 123 --approve --body "LGTM!"
gh pr review 123 --request-changes --body "Fix please"
gh pr review 123 --comment --body "Thoughts..."

# Checks
gh pr checks 123 --watch --interval 5

# Comment
gh pr comment 123 --body "Nice!"
gh pr comment 123 --edit 456789 --body "Updated"

# Update branch
gh pr update-branch 123 --force

# Lock/Unlock
gh pr lock 123 --reason off-topic | gh pr unlock 123

# Revert
gh pr revert 123 --branch revert-pr-123

# Status
gh pr status
```

## GitHub Actions

```shell
# Workflow runs
gh run list --workflow ci.yml --branch main --limit 20
gh run view 123456789 --log
gh run watch 123456789 --interval 5
gh run rerun 123456789
gh run cancel 123456789
gh run delete 123456789
gh run download 123456789 --name build --dir ./artifacts

# Workflows
gh workflow list
gh workflow view ci.yml --yaml
gh workflow enable ci.yml | gh workflow disable ci.yml
gh workflow run ci.yml --raw-field version="1.0.0" --ref develop

# Caches
gh cache list --branch main --limit 50
gh cache delete 123456789 | gh cache delete --all

# Secrets
gh secret list
echo "$MY_SECRET" | gh secret set MY_SECRET
gh secret set MY_SECRET --env production
gh secret delete MY_SECRET

# Variables
gh variable list
gh variable set MY_VAR "value" --env production
gh variable get MY_VAR
gh variable delete MY_VAR
```

## Projects

```shell
gh project list --owner owner --open
gh project view 123 --format json
gh project create --title "Project" --org orgname
gh project edit 123 --title "New Title"
gh project delete 123
gh project field-list 123
gh project field-create 123 --title "Status" --datatype single_select
gh project field-delete 123 --id 456
gh project item-list 123
gh project item-create 123 --title "New item"
gh project item-add 123 --owner owner --repo repo --issue 456
gh project item-edit 123 --id 456 --title "Updated"
gh project item-delete 123 --id 456
gh project item-archive 123 --id 456
gh project copy 123 --owner target-owner --title "Copy"
gh project mark-template 123
```

## Releases

```shell
gh release list
gh release view v1.0.0
gh release create v1.0.0 --notes "Release notes" --notes-file notes.md \
  --draft --prerelease --title "Version 1.0.0" --target main
gh release upload v1.0.0 ./file.tar.gz --clobber
gh release download v1.0.0 --pattern "*.tar.gz" --dir ./downloads --archive zip
gh release edit v1.0.0 --notes "Updated"
gh release delete v1.0.0 --yes
gh release delete-asset v1.0.0 file.tar.gz
gh release verify v1.0.0
gh release verify-asset v1.0.0 file.tar.gz
```

## Gists

```shell
gh gist list --public --limit 20
gh gist view abc123 --files
gh gist create script.py --desc "Script" --public
gh gist edit abc123
gh gist delete abc123
gh gist rename abc123 --filename old.py new.py
gh gist clone abc123 my-directory
```

## Codespaces

```shell
gh codespace list
gh codespace create --repo owner/repo --branch develop
gh codespace view
gh codespace ssh --command "ls"
gh codespace code --codec
gh codespace stop | gh codespace delete
gh codespace logs --tail 100
gh codespace ports
gh codespace cp file.txt :/workspaces/file.txt
gh codespace rebuild
```

## Search

```shell
gh search code "TODO" --repo owner/repo --extension py
gh search commits "fix bug"
gh search issues "label:bug state:open"
gh search prs "is:open is:pr review:required"
gh search repos "stars:>1000 language:python" --limit 50 --order desc --sort stars
```

## API Requests

```shell
# REST
gh api /user
gh api --method POST /repos/owner/repo/issues --field title="Issue" --field body="Body"
gh api /user --paginate --jq '.login'
gh api /user --include --silent --raw

# GraphQL
gh api graphql -f query='{ viewer { login } }'
```

## Other Commands

```shell
# Browse
gh browse main.go:312 --branch bug-fix --no-browser
gh browse --actions | --projects | --releases | --settings | --wiki

# Labels
gh label list
gh label create bug --color "d73a4a" --description "Bug"
gh label edit bug --name "bug-report"
gh label delete bug
gh label clone owner/repo

# SSH/GPG Keys
gh ssh-key list | gh ssh-key add ~/.ssh/id_rsa.pub --title "laptop"
gh gpg-key list | gh gpg-key add ~/.ssh/id_rsa.pub

# Organizations
gh org list --user username --json login
gh org view orgname --json members

# Rulessets
gh ruleset list
gh ruleset view 123
gh ruleset check --branch feature --repo owner/repo

# Attestations
gh attestation download owner/repo --artifact-id 123456
gh attestation verify owner/repo

# Extensions
gh extension list | gh extension search github
gh extension install owner/repo
gh extension upgrade name
gh extension remove name

# Aliases
gh alias list
gh alias set prview 'pr view --web'
gh alias delete prview

# Completion
gh completion -s terminal > ~/.gh-complete.terminal
gh completion -s powershell > ~/.gh-complete.ps1

# Status
gh status

# Preview
gh preview
gh preview prompter

# Agent Tasks
gh agent-task list
gh agent-task view 123
gh agent-task create --description "Task"
```

## Global Flags

`--help`, `--version`, `--repo owner/repo`, `--hostname HOST`, `--jq EXPRESSION`, `--json FIELDS`, `--template STRING`, `--web`, `--paginate`, `--verbose`, `--debug`, `--timeout SECONDS`

## Output Formatting

```shell
# JSON
gh repo view --json name,description
gh pr list --json number,title --jq '.[] | select(.number > 100)'

# Template
gh repo view --template '{{.name}}: {{.description}}'
```

## Common Workflows

```shell
# Create PR from issue
gh issue develop 123 --branch feature/issue-123
git commit -m "Fix #123"
gh pr create --title "Fix #123" --body "Closes #123"

# Bulk operations
gh issue list --search "label:stale" --json number --jq '.[].number' | xargs -I {} gh issue close {} --comment "Closing"

# Fork sync
gh repo fork original/repo --clone
gh repo sync

# Repo setup
gh repo create my-project --public --clone --gitignore python --license mit
gh label create bug --color "d73a4a"
gh label create enhancement --color "a2eeef"
```

References: https://cli.github.com/manual/ | https://docs.github.com/en/github-cli
