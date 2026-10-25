#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""GitHub / GitLab integration tool.

Performs repository operations (issues, pull/merge requests, actions,
releases, gists) via the provider API using configured credentials.
"""

import json
import os
from typing import Any

import httpx

from encre.tools.base import build_tool

_GITHUB_API = "https://api.github.com"
_GITLAB_API = "https://gitlab.com/api/v4"


def _get_token(platform: str) -> str:
    """Get token.

    Args:
        platform: Description of the platform parameter.
    """
    if platform == "gitlab":
        return os.environ.get("GITLAB_TOKEN", os.environ.get("GITLAB_PRIVATE_TOKEN", ""))
    return os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))


async def _github_execute(**kwargs: Any) -> str:
    """Github execute.

    Args:
        kwargs: Description of the kwargs parameter.
    """
    action = kwargs.get("action", "")
    platform = kwargs.get("platform", "github")
    repo = kwargs.get("repo", "")
    token = kwargs.get("token", "") or _get_token(platform)

    if not token:
        return f"Error: No {platform} token configured. Set {platform.upper()}_TOKEN environment variable."

    api_base = _GITHUB_API if platform == "github" else _GITLAB_API
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json" if platform == "github" else "application/json",
        "User-Agent": "encre-agent",
    }

    async with httpx.AsyncClient(base_url=api_base, headers=headers, timeout=httpx.Timeout(60.0)) as client:

        # ── Issues ─────────────────────────────────────────────────────
        if action == "issues_list":
            state = kwargs.get("state", "open")
            params = {"state": state, "per_page": min(kwargs.get("per_page", 30), 100)}
            resp = await client.get(f"/repos/{repo}/issues", params=params)
            if resp.is_error:
                return _error(resp)
            data = resp.json()
            out = [f"#{i['number']} ({i['state']}): {i['title']}" for i in data]
            return f"Issues for {repo}:\n" + "\n".join(out) if out else f"No issues found for {repo}."

        elif action == "issues_create":
            title = kwargs.get("title", "")
            body = kwargs.get("body", "")
            labels = kwargs.get("labels", [])
            if not title:
                return "Error: 'title' is required for issues_create."
            payload: dict[str, Any] = {"title": title}
            if body:
                payload["body"] = body
            if labels:
                payload["labels"] = labels if isinstance(labels, list) else [labels]
            resp = await client.post(f"/repos/{repo}/issues", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"Issue created: #{d['number']} - {d['title']}\n{d['html_url']}"

        elif action == "issues_update":
            issue_number = kwargs.get("issue_number", 0)
            payload = {}
            for field in ("title", "body", "state", "labels"):
                val = kwargs.get(field)
                if val is not None:
                    payload[field] = val
            if not issue_number:
                return "Error: 'issue_number' is required."
            resp = await client.patch(f"/repos/{repo}/issues/{issue_number}", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"Issue #{d['number']} updated: {d['state']} - {d['title']}\n{d['html_url']}"

        elif action == "issues_get":
            issue_number = kwargs.get("issue_number", 0)
            if not issue_number:
                return "Error: 'issue_number' is required."
            resp = await client.get(f"/repos/{repo}/issues/{issue_number}")
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return json.dumps({
                "number": d["number"],
                "title": d["title"],
                "state": d["state"],
                "body": d.get("body", ""),
                "labels": [lb["name"] for lb in d.get("labels", [])],
                "assignees": [a["login"] for a in d.get("assignees", [])],
                "comments": d.get("comments", 0),
                "created_at": d.get("created_at", ""),
                "updated_at": d.get("updated_at", ""),
                "html_url": d.get("html_url", ""),
            }, indent=2)

        elif action == "issues_search":
            query = kwargs.get("query", "")
            if not query:
                return "Error: 'query' is required for issues_search."
            params = {"q": query, "per_page": min(kwargs.get("per_page", 10), 100)}
            resp = await client.get("/search/issues", params=params)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            items = d.get("items", [])
            if not items:
                return "No matching issues found."
            out = [f"#{i['number']} ({i['state']}) [{i['repository_url'].split('/')[-1]}]: {i['title']}" for i in items]
            return f"Found {d['total_count']} issues:\n" + "\n".join(out)

        # ── Pull Requests ─────────────────────────────────────────────
        elif action == "prs_list":
            state = kwargs.get("state", "open")
            params = {"state": state, "per_page": min(kwargs.get("per_page", 30), 100)}
            resp = await client.get(f"/repos/{repo}/pulls", params=params)
            if resp.is_error:
                return _error(resp)
            data = resp.json()
            out = [f"!{p['number']} ({p['state']}): {p['title']} by @{p['user']['login']}" for p in data]
            return f"Pull requests for {repo}:\n" + "\n".join(out) if out else f"No PRs found for {repo}."

        elif action == "prs_create":
            title = kwargs.get("title", "")
            head = kwargs.get("head", "")
            base = kwargs.get("base", "main")
            body = kwargs.get("body", "")
            if not title or not head:
                return "Error: 'title' and 'head' are required for prs_create."
            payload = {"title": title, "head": head, "base": base}
            if body:
                payload["body"] = body
            resp = await client.post(f"/repos/{repo}/pulls", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"PR created: !{d['number']} - {d['title']}\n{d['html_url']}"

        elif action == "prs_get":
            pr_number = kwargs.get("pr_number", 0)
            if not pr_number:
                return "Error: 'pr_number' is required."
            resp = await client.get(f"/repos/{repo}/pulls/{pr_number}")
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return json.dumps({
                "number": d["number"],
                "title": d["title"],
                "state": d["state"],
                "body": d.get("body", ""),
                "user": d["user"]["login"],
                "head": d["head"]["ref"],
                "base": d["base"]["ref"],
                "mergeable": d.get("mergeable"),
                "merged": d.get("merged", False),
                "commits": d.get("commits", 0),
                "changed_files": d.get("changed_files", 0),
                "additions": d.get("additions", 0),
                "deletions": d.get("deletions", 0),
                "created_at": d.get("created_at", ""),
                "html_url": d.get("html_url", ""),
            }, indent=2)

        elif action == "prs_merge":
            pr_number = kwargs.get("pr_number", 0)
            merge_method = kwargs.get("merge_method", "merge")
            if not pr_number:
                return "Error: 'pr_number' is required."
            payload = {"merge_method": merge_method}
            resp = await client.put(f"/repos/{repo}/pulls/{pr_number}/merge", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"PR !{pr_number} merged: {d.get('message', 'success')}"

        elif action == "prs_review":
            pr_number = kwargs.get("pr_number", 0)
            review_body = kwargs.get("body", "")
            review_event = kwargs.get("event", "COMMENT")
            if not pr_number:
                return "Error: 'pr_number' is required."
            if platform == "github":
                payload = {"body": review_body, "event": review_event}
                resp = await client.post(f"/repos/{repo}/pulls/{pr_number}/reviews", json=payload)
            else:
                payload = {"body": review_body}
                resp = await client.post(f"/projects/{repo}/merge_requests/{pr_number}/notes", json=payload)
            if resp.is_error:
                return _error(resp)
            return f"Review submitted on PR !{pr_number}"

        elif action == "prs_files":
            pr_number = kwargs.get("pr_number", 0)
            if not pr_number:
                return "Error: 'pr_number' is required."
            resp = await client.get(f"/repos/{repo}/pulls/{pr_number}/files")
            if resp.is_error:
                return _error(resp)
            files = resp.json()
            out = [f"{f['status']}: {f['filename']} (+{f.get('additions',0)}/-{f.get('deletions',0)})" for f in files]
            return "\n".join(out) if out else "No files changed."

        elif action == "prs_commits":
            pr_number = kwargs.get("pr_number", 0)
            if not pr_number:
                return "Error: 'pr_number' is required."
            resp = await client.get(f"/repos/{repo}/pulls/{pr_number}/commits")
            if resp.is_error:
                return _error(resp)
            commits = resp.json()
            out = [f"{c['sha'][:8]} {c['commit']['message'].split(chr(10))[0]} by @{c['commit']['author']['name']}" for c in commits]
            return "\n".join(out) if out else "No commits."

        # ── Repositories ───────────────────────────────────────────────
        elif action == "repo_search":
            query = kwargs.get("query", "")
            if not query:
                return "Error: 'query' is required for repo_search."
            params = {"q": query, "per_page": min(kwargs.get("per_page", 10), 100)}
            resp = await client.get("/search/repositories", params=params)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            items = d.get("items", [])
            if not items:
                return "No repositories found."
            out = [f"{r['full_name']} ({r['description'] or 'no description'}) stars: {r['stargazers_count']}" for r in items]
            return f"Found {d['total_count']} repos:\n" + "\n".join(out)

        elif action == "repo_get":
            if not repo:
                return "Error: 'repo' is required (format: owner/repo)."
            resp = await client.get(f"/repos/{repo}")
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return json.dumps({
                "name": d["full_name"],
                "description": d.get("description", ""),
                "stars": d.get("stargazers_count", 0),
                "forks": d.get("forks_count", 0),
                "language": d.get("language", ""),
                "topics": d.get("topics", []),
                "license": d.get("license", {}).get("spdx_id", "") if d.get("license") else "",
                "open_issues": d.get("open_issues_count", 0),
                "default_branch": d.get("default_branch", ""),
                "created_at": d.get("created_at", ""),
                "updated_at": d.get("updated_at", ""),
                "html_url": d.get("html_url", ""),
            }, indent=2)

        elif action == "repo_create":
            name = kwargs.get("name", "")
            description = kwargs.get("description", "")
            private = kwargs.get("private", False)
            if not name:
                return "Error: 'name' is required for repo_create."
            payload = {"name": name, "description": description, "private": private}
            resp = await client.post("/user/repos", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"Repository created: {d['full_name']}\n{d['html_url']}"

        elif action == "repo_list":
            params = {"per_page": min(kwargs.get("per_page", 30), 100), "sort": "updated"}
            resp = await client.get("/user/repos", params=params)
            if resp.is_error:
                return _error(resp)
            repos = resp.json()
            out = [f"{r['full_name']} ({r.get('language','') or 'N/A'}) stars: {r['stargazers_count']}" for r in repos]
            return "\n".join(out) if out else "No repositories found."

        # ── Releases ───────────────────────────────────────────────────
        elif action == "releases_list":
            params = {"per_page": min(kwargs.get("per_page", 10), 100)}
            endpoint = f"/repos/{repo}/releases"
            resp = await client.get(endpoint, params=params)
            if resp.is_error:
                return _error(resp)
            releases = resp.json()
            out = [f"{r['tag_name']} - {r['name'] or 'no name'} ({r.get('published_at','')[:10]})" for r in releases]
            return "\n".join(out) if out else f"No releases for {repo}."

        # ── Gists ──────────────────────────────────────────────────────
        elif action == "gists_list":
            params = {"per_page": min(kwargs.get("per_page", 30), 100)}
            resp = await client.get("/gists", params=params)
            if resp.is_error:
                return _error(resp)
            gists = resp.json()
            out = [f"{g['id'][:8]} {g.get('description','no description')} ({len(g['files'])} files)" for g in gists]
            return "\n".join(out) if out else "No gists found."

        elif action == "gists_create":
            description = kwargs.get("description", "")
            files_data = kwargs.get("files", {})
            public = kwargs.get("public", False)
            if not files_data:
                return "Error: 'files' is required for gists_create (dict of {filename: content})."
            payload = {
                "description": description,
                "public": public,
                "files": {fname: {"content": content} for fname, content in files_data.items()},
            }
            resp = await client.post("/gists", json=payload)
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return f"Gist created: {d['html_url']}"

        # ── Actions ────────────────────────────────────────────────────
        elif action == "actions_list_workflows":
            params = {"per_page": min(kwargs.get("per_page", 30), 100)}
            resp = await client.get(f"/repos/{repo}/actions/workflows", params=params)
            if resp.is_error:
                return _error(resp)
            workflows = resp.json().get("workflows", [])
            out = [f"{w['name']} ({w['state']}) - {w.get('path','')}" for w in workflows]
            return "\n".join(out) if out else f"No workflows in {repo}."

        elif action == "actions_dispatch":
            workflow_id = kwargs.get("workflow_id", "")
            ref = kwargs.get("ref", "main")
            inputs = kwargs.get("inputs", {})
            if not workflow_id:
                return "Error: 'workflow_id' is required for actions_dispatch."
            payload = {"ref": ref}
            if inputs:
                payload["inputs"] = inputs
            resp = await client.post(f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches", json=payload)
            if resp.is_error:
                return _error(resp)
            return f"Workflow dispatch triggered for {workflow_id} on {ref}."

        elif action == "actions_list_runs":
            params = {"per_page": min(kwargs.get("per_page", 10), 100)}
            resp = await client.get(f"/repos/{repo}/actions/runs", params=params)
            if resp.is_error:
                return _error(resp)
            runs = resp.json().get("workflow_runs", [])
            out = [f"#{r['run_number']} ({r['status']}/{r['conclusion']}) {r['name']} - {r['created_at'][:19]}" for r in runs]
            return "\n".join(out) if out else f"No workflow runs in {repo}."

        # ── Metadata ───────────────────────────────────────────────────
        elif action == "user_info":
            resp = await client.get("/user")
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            return json.dumps({
                "login": d.get("login", ""),
                "name": d.get("name", ""),
                "email": d.get("email", ""),
                "public_repos": d.get("public_repos", 0),
                "followers": d.get("followers", 0),
                "following": d.get("following", 0),
                "html_url": d.get("html_url", ""),
            }, indent=2)

        elif action == "rate_limit":
            resp = await client.get("/rate_limit")
            if resp.is_error:
                return _error(resp)
            d = resp.json()
            core = d.get("resources", {}).get("core", {})
            remaining = core.get("remaining", "?")
            limit = core.get("limit", "?")
            reset_at = core.get("reset", "")
            return f"API Rate Limit:\nCore: {remaining}/{limit} remaining\nResets at: {reset_at}"

        else:
            return f"Error: Unknown action '{action}'. See tool description for available actions."


def _error(resp: httpx.Response) -> str:
    """Error.

    Args:
        resp: Description of the resp parameter.
    """
    try:
        detail = resp.json()
        msg = detail.get("message", resp.text)
    except Exception:
        msg = resp.text
    return f"API error ({resp.status_code}): {msg[:500]}"


git_hub_tool = build_tool(
    name="github",
    description=(
        "Interact with the GitHub and GitLab REST APIs to manage issues, pull/merge "
        "requests, repositories, releases, gists, and CI Actions runs. "
        "Use this for any provider-level operation such as listing/creating/searching "
        "issues, opening or merging PRs, reviewing code, dispatching workflows, or "
        "browsing repo metadata. "
        "Do NOT use this for local git commands (commit, push, branch) - use the git "
        "tool instead; and avoid it for in-depth file diffs already covered by git. "
        "Tips: prefer GITHUB_TOKEN/GITLAB_TOKEN env vars over passing `token`; cap "
        "`per_page` (max 100) to keep responses small. "
        "Pitfalls: most repo-scoped actions require `repo` as owner/repo; the request "
        "times out after 60s and provider rate limits apply."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The provider operation to execute. One of: issues_list, issues_create, issues_update, issues_get, issues_search; prs_list, prs_create, prs_get, prs_merge, prs_review, prs_files, prs_commits; repo_search, repo_get, repo_create, repo_list; releases_list; gists_list, gists_create; actions_list_workflows, actions_dispatch, actions_list_runs; user_info; rate_limit.",
            },
            "platform": {
                "type": "string",
                "enum": ["github", "gitlab"],
                "description": "Target platform; selects the matching API base URL and token env var. Defaults to github.",
            },
            "repo": {
                "type": "string",
                "description": "Repository identifier in owner/repo form (e.g. 'octocat/Hello-World'); required for most repo-scoped actions.",
            },
            "token": {
                "type": "string",
                "description": "Personal access token used for Authorization; if omitted, falls back to GITHUB_TOKEN/GH_TOKEN or GITLAB_TOKEN/GITLAB_PRIVATE_TOKEN env vars.",
            },
            "issue_number": {
                "type": "integer",
                "description": "Numeric issue identifier; required by issues_get and issues_update.",
            },
            "pr_number": {
                "type": "integer",
                "description": "Numeric pull/merge request identifier; required by prs_get, prs_merge, prs_review, prs_files, prs_commits.",
            },
            "title": {
                "type": "string",
                "description": "Title text used when creating an issue or pull request.",
            },
            "body": {
                "type": "string",
                "description": "Markdown body content for issues, PRs, reviews, or gists.",
            },
            "head": {
                "type": "string",
                "description": "Source branch (head) to merge from when creating a pull request.",
            },
            "base": {
                "type": "string",
                "description": "Target branch (base) to merge into; defaults to 'main' when omitted.",
            },
            "state": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "description": "Lifecycle filter for listing issues or PRs.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of label names to attach when creating/updating issues.",
            },
            "query": {
                "type": "string",
                "description": "Search query string for issues_search or repo_search (supports GitHub search syntax).",
            },
            "name": {
                "type": "string",
                "description": "New repository name (without owner) for repo_create.",
            },
            "description": {
                "type": "string",
                "description": "Human-readable description for a new repo or gist.",
            },
            "private": {
                "type": "boolean",
                "description": "If true, create the repository as private.",
            },
            "files": {
                "type": "object",
                "description": "Mapping of {filename: file_content} for gists_create; at least one entry is required.",
            },
            "public": {
                "type": "boolean",
                "description": "If true, create the gist as publicly discoverable.",
            },
            "workflow_id": {
                "type": "string",
                "description": "Workflow ID or filename (e.g. 'deploy.yml') for actions_dispatch.",
            },
            "ref": {
                "type": "string",
                "description": "Git ref (branch or tag) on which to dispatch the workflow; defaults to 'main'.",
            },
            "inputs": {
                "type": "object",
                "description": "Key-value inputs passed to the dispatched workflow (must match workflow input declarations).",
            },
            "merge_method": {
                "type": "string",
                "enum": ["merge", "squash", "rebase"],
                "description": "Strategy used when merging a pull request.",
            },
            "event": {
                "type": "string",
                "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                "description": "Type of review to submit (GitHub only).",
            },
            "per_page": {
                "type": "integer",
                "description": "Page size for list endpoints; clamped to a maximum of 100.",
            },
        },
        "required": ["action"],
    },
    execute=_github_execute,
    intents=["coding", "system", "general"],
    category="code_intel",
    semantic_type="network",
    is_concurrency_safe=lambda data: data.get("action") in (
        "issues_list", "prs_list", "repo_search", "repo_get", "repo_list",
        "releases_list", "gists_list", "actions_list_workflows", "actions_list_runs",
        "rate_limit", "user_info", "issues_search",
    ),
    is_destructive=True,
)
