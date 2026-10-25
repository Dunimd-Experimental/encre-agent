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

"""MCP server provider catalog.

Each entry is a real service provider that offers an MCP server, with its
config in the standard ``.mcp.json`` format.  The frontend uses this catalog
to let users pick a provider and auto-fill the configuration form.

To extend: add to ``MCP_PROVIDERS`` below.
"""

from typing import Any

MCP_PROVIDERS: list[dict[str, Any]] = [
    # ── GitHub ──────────────────────────────────────────────────────────
    {
        "id": "github",
        "label": "GitHub",
        "description": "GitHub API -- manage repositories, issues, pull requests, code search, and Actions",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
        },
        "env_fields": {"GITHUB_TOKEN": {"label": "GitHub Personal Access Token", "secret": True}},
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    },
    # ── GitLab ──────────────────────────────────────────────────────────
    {
        "id": "gitlab",
        "label": "GitLab",
        "description": "GitLab API -- manage projects, merge requests, issues, pipelines, and registries",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-server-gitlab"],
        },
        "env_fields": {
            "GITLAB_TOKEN": {"label": "GitLab Personal Access Token", "secret": True},
            "GITLAB_URL": {"label": "GitLab Instance URL (default: https://gitlab.com)", "secret": False},
        },
        "docs": "https://github.com/manuelmhtr/mcp-server-gitlab",
    },
    # ── Brave Search ────────────────────────────────────────────────────
    {
        "id": "brave-search",
        "label": "Brave Search",
        "description": "Web search via Brave Search API",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-brave-search"],
        },
        "env_fields": {"BRAVE_API_KEY": {"label": "Brave Search API Key", "secret": True}},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/brave-search",
    },
    # ── Stripe ──────────────────────────────────────────────────────────
    {
        "id": "stripe",
        "label": "Stripe",
        "description": "Stripe payment platform -- customers, products, invoices, payment links, balance",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@stripe/mcp", "--tools=all"],
        },
        "env_fields": {"STRIPE_SECRET_KEY": {"label": "Stripe Secret Key (sk_...)", "secret": True}},
        "docs": "https://github.com/stripe/ai",
    },
    # ── Supabase ────────────────────────────────────────────────────────
    {
        "id": "supabase",
        "label": "Supabase",
        "description": "Supabase project management -- database, storage, functions, auth",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@supabase/mcp-server-supabase@latest", "--read-only"],
        },
        "env_fields": {"SUPABASE_ACCESS_TOKEN": {"label": "Supabase Personal Access Token (sbp_...)", "secret": True}},
        "docs": "https://supabase.com/docs/guides/integration/mcp",
    },
    # ── Vercel ──────────────────────────────────────────────────────────
    {
        "id": "vercel",
        "label": "Vercel",
        "description": "Vercel platform -- deployments, environment variables, project management",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "--package", "@vercel/sdk", "--", "mcp", "start"],
        },
        "env_fields": {"VERCEL_TOKEN": {"label": "Vercel API Token", "secret": True}},
        "docs": "https://www.npmjs.com/package/@vercel/sdk",
    },
    # ── Cloudflare ──────────────────────────────────────────────────────
    {
        "id": "cloudflare",
        "label": "Cloudflare",
        "description": "Cloudflare -- Workers, DNS, KV, analytics, WAF configuration",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["mcp-remote", "https://remote-mcp-server.your-account.workers.dev/sse"],
        },
        "env_fields": {"CLOUDFLARE_API_TOKEN": {"label": "Cloudflare API Token", "secret": True}},
        "docs": "https://github.com/cloudflare/mcp-server-cloudflare",
    },
    # ── Sentry ──────────────────────────────────────────────────────────
    {
        "id": "sentry",
        "label": "Sentry",
        "description": "Sentry error tracking -- issues, events, performance monitoring",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@getsentry/sentry-mcp-stdio"],
        },
        "env_fields": {"SENTRY_AUTH_TOKEN": {"label": "Sentry Auth Token", "secret": True}},
        "docs": "https://github.com/getsentry/sentry-mcp-stdio",
    },
    # ── Prisma ──────────────────────────────────────────────────────────
    {
        "id": "prisma",
        "label": "Prisma",
        "description": "Prisma ORM -- schema management, migrations, database queries, Prisma Postgres",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "prisma", "mcp"],
        },
        "env_fields": {},
        "docs": "https://www.prismagraphql.com/blog/prisma-orm-6-6-0-esm-support-d1-migrations-and-prisma-mcp-server",
    },
    # ── Notion ──────────────────────────────────────────────────────────
    {
        "id": "notion",
        "label": "Notion",
        "description": "Notion workspace -- semantic search, pages, databases",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
        },
        "env_fields": {"NOTION_TOKEN": {"label": "Notion Integration Token (ntn_...)", "secret": True}},
        "docs": "https://www.npmjs.com/package/@notionhq/notion-mcp-server",
    },
    # ── Figma ───────────────────────────────────────────────────────────
    {
        "id": "figma",
        "label": "Figma",
        "description": "Figma design -- read file data, extract components, styles, and assets",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "figma-developer-mcp", "--stdio"],
        },
        "env_fields": {"FIGMA_API_KEY": {"label": "Figma Personal Access Token", "secret": True}},
        "docs": "https://github.com/GLips/Figma-Context-MCP",
    },
    # ── Slack ───────────────────────────────────────────────────────────
    {
        "id": "slack",
        "label": "Slack",
        "description": "Slack workspace -- read messages, channels, users, and post messages",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-slack"],
        },
        "env_fields": {
            "SLACK_BOT_TOKEN": {"label": "Slack Bot Token (xoxb-...)", "secret": True},
            "SLACK_TEAM_ID": {"label": "Slack Team ID", "secret": False},
        },
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/slack",
    },
    # ── Obsidian ────────────────────────────────────────────────────────
    {
        "id": "obsidian",
        "label": "Obsidian",
        "description": "Obsidian vault access -- read, write, search notes, manage tags",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@bitbonsai/mcpvault@latest"],
        },
        "env_fields": {},
        "docs": "https://github.com/bitbonsai/mcpvault",
    },
    # ══════════════════════════════════════════════════════════════════════
    # NEW — 2026 Q2 additions
    # ══════════════════════════════════════════════════════════════════════
    # ── Filesystem ──────────────────────────────────────────────────────
    {
        "id": "filesystem",
        "label": "Filesystem",
        "description": "Sandboxed filesystem access -- read, write, search, move files in allowed directories",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
        "env_fields": {},
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    },
    # ── PostgreSQL ──────────────────────────────────────────────────────
    {
        "id": "postgres",
        "label": "PostgreSQL",
        "description": "PostgreSQL database -- query, read schema, introspect tables directly",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-postgres-tools"],
        },
        "env_fields": {"DATABASE_URL": {"label": "PostgreSQL connection string (postgresql://...)", "secret": True}},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/postgres",
    },
    # ── SQLite ──────────────────────────────────────────────────────────
    {
        "id": "sqlite",
        "label": "SQLite",
        "description": "SQLite database -- query, create tables, introspect local .db files",
        "config": {
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-sqlite", "--db-path", "./data.db"],
        },
        "env_fields": {},
        "docs": "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
    },
    # ── Docker ──────────────────────────────────────────────────────────
    {
        "id": "docker",
        "label": "Docker",
        "description": "Docker container management -- list, inspect, exec commands in containers",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-docker"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/docker",
    },
    # ── Kubernetes ──────────────────────────────────────────────────────
    {
        "id": "kubernetes",
        "label": "Kubernetes",
        "description": "Kubernetes cluster management -- pods, deployments, services, logs (uses current kubeconfig)",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-kubernetes"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/kubernetes",
    },
    # ── AWS ─────────────────────────────────────────────────────────────
    {
        "id": "aws",
        "label": "AWS",
        "description": "AWS services -- S3, EC2, Lambda, CloudWatch, IAM (uses AWS credentials from env)",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-aws"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/aws",
    },
    # ── Google Cloud ────────────────────────────────────────────────────
    {
        "id": "gcp",
        "label": "Google Cloud",
        "description": "Google Cloud Platform -- GCS, BigQuery, Cloud Run, GKE (uses gcloud credentials)",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-gcp"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/gcp",
    },
    # ── Jira ────────────────────────────────────────────────────────────
    {
        "id": "jira",
        "label": "Jira",
        "description": "Atlassian Jira -- issues, sprints, projects, epics, search",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-server-jira"],
        },
        "env_fields": {
            "JIRA_API_TOKEN": {"label": "Jira API Token", "secret": True},
            "JIRA_URL": {"label": "Jira Instance URL (https://your-domain.atlassian.net)", "secret": False},
            "JIRA_USER": {"label": "Jira Email", "secret": False},
        },
        "docs": "https://github.com/sooperset/mcp-atlassian",
    },
    # ── Linear ──────────────────────────────────────────────────────────
    {
        "id": "linear",
        "label": "Linear",
        "description": "Linear project management -- issues, cycles, projects, teams",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-linear"],
        },
        "env_fields": {"LINEAR_API_KEY": {"label": "Linear API Key", "secret": True}},
        "docs": "https://github.com/ibraheem4/mcp-linear",
    },
    # ── Sequential Thinking ─────────────────────────────────────────────
    {
        "id": "sequential-thinking",
        "label": "Sequential Thinking",
        "description": "Chain-of-thought reasoning -- break down complex problems step by step",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-sequential-thinking"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/sequential-thinking",
    },
    # ── Tavily (web search) ─────────────────────────────────────────────
    {
        "id": "tavily",
        "label": "Tavily Search",
        "description": "Web search API optimized for AI agents -- news, general, and deep research queries",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-tavily"],
        },
        "env_fields": {"TAVILY_API_KEY": {"label": "Tavily API Key", "secret": True}},
        "docs": "https://github.com/tavily-ai/tavily-mcp",
    },
    # ── Airtable ────────────────────────────────────────────────────────
    {
        "id": "airtable",
        "label": "Airtable",
        "description": "Airtable workspace -- read, query, create, update bases and records",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-server-airtable"],
        },
        "env_fields": {"AIRTABLE_TOKEN": {"label": "Airtable Personal Access Token", "secret": True}},
        "docs": "https://github.com/domdomegg/airtable-mcp-server",
    },
    # ── Redis ───────────────────────────────────────────────────────────
    {
        "id": "redis",
        "label": "Redis",
        "description": "Redis in-memory store -- get, set, delete keys, list operations (default localhost:6379)",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-server-redis"],
        },
        "env_fields": {"REDIS_URL": {"label": "Redis connection string (redis://localhost:6379)", "secret": True}},
        "docs": "https://github.com/prajwalnayak7/mcp-server-redis",
    },
    # ── Elasticsearch ───────────────────────────────────────────────────
    {
        "id": "elasticsearch",
        "label": "Elasticsearch",
        "description": "Elasticsearch -- index management, search, document CRUD, cluster health",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-elasticsearch"],
        },
        "env_fields": {
            "ES_HOST": {"label": "Elasticsearch host (http://localhost:9200)", "secret": False},
            "ES_API_KEY": {"label": "Elasticsearch API Key", "secret": True},
        },
        "docs": "https://github.com/cr7258/elasticsearch-mcp-server",
    },
    # ── ClickUp ─────────────────────────────────────────────────────────
    {
        "id": "clickup",
        "label": "ClickUp",
        "description": "ClickUp project management -- tasks, lists, folders, spaces, docs",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-server-clickup"],
        },
        "env_fields": {"CLICKUP_API_KEY": {"label": "ClickUp API Token (pk_...)", "secret": True}},
        "docs": "https://github.com/nguyenvanduocit/clickup-mcp-server",
    },
    # ── YouTube ─────────────────────────────────────────────────────────
    {
        "id": "youtube",
        "label": "YouTube",
        "description": "YouTube -- search videos, get captions/transcripts, channel and playlist info",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-youtube"],
        },
        "env_fields": {"YOUTUBE_API_KEY": {"label": "YouTube Data API Key", "secret": True}},
        "docs": "https://github.com/ZubeidHendricks/mcp-youtube",
    },
    # ── Hacker News ─────────────────────────────────────────────────────
    {
        "id": "hackernews",
        "label": "Hacker News",
        "description": "Hacker News -- top stories, comments, user profiles, search (no API key needed)",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-hackernews"],
        },
        "env_fields": {},
        "docs": "https://github.com/anthropics/anthropic-quickstarts/tree/main/mcp/hackernews",
    },
    # ── Exa Search ──────────────────────────────────────────────────────
    {
        "id": "exa",
        "label": "Exa (Web Search)",
        "description": "Exa web search API -- neural search, content extraction, similar pages",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mcp-exa"],
        },
        "env_fields": {"EXA_API_KEY": {"label": "Exa API Key", "secret": True}},
        "docs": "https://github.com/exa-labs/exa-mcp-server",
    },
    # ── Mem0 (memory layer) ─────────────────────────────────────────────
    {
        "id": "mem0",
        "label": "Mem0 (Memory)",
        "description": "Persistent memory layer -- store, retrieve, search user preferences and facts across sessions",
        "config": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "mem0-mcp"],
        },
        "env_fields": {"MEM0_API_KEY": {"label": "Mem0 API Key", "secret": True}},
        "docs": "https://github.com/mem0ai/mem0-mcp",
    },
]


def get_mcp_provider(provider_id: str) -> dict[str, Any] | None:
    """Return the provider entry for ``provider_id`` or None if unknown."""
    for p in MCP_PROVIDERS:
        # Linear scan is fine: the catalog is a small, fixed list.
        if p["id"] == provider_id:
            return p
    return None


def mcp_catalog_payload() -> dict[str, Any]:
    """Serializable snapshot used by the frontend MCP form."""
    return {"providers": MCP_PROVIDERS}


__all__ = [
    "MCP_PROVIDERS",
    "get_mcp_provider",
    "mcp_catalog_payload",
]
