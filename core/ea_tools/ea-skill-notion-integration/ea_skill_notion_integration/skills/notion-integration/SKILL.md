---
name: notion-integration
description: "Notion integration - pages, databases, and block management via the Notion API."
aliases: [notion-integration, notion-api, notion]
argument_hint: "What you want to do with Notion"
user_invocable: true
hidden: true
---

# notion-integration
# SKILL.md — Notion Integration

## Metadata

- **Name:** notion
- **Description:** Integrate with Notion's API v1 to search pages and databases, read and create content, manage database entries, add comments, and archive pages.
- **Trigger Phrases:** "search Notion", "find Notion page", "create Notion page", "query Notion database", "get Notion content", "Notion integration", "add Notion comment", "update Notion properties", "Notion API", "archive Notion page", "Notion database", "sync to Notion", "Notion block", "read Notion page", "Notion token"
- **Version:** 2.0.0

---

## 1. Capabilities

This skill enables the following operations against the Notion API v1:

1. **Search pages and databases** — Full-text search across a workspace via `POST /search`
2. **Get page content** — Retrieve page metadata and all blocks via `GET /pages/{id}` and `GET /blocks/{id}/children`
3. **Create a page** — Create a new page under a parent (page or database) via `POST /pages`
4. **Update page properties** — Patch title, status, select, date, checkbox, and other property types via `PATCH /pages/{id}`
5. **Create a database** — Create a new database under a parent page via `POST /databases`
6. **Query a database** — Filter, sort, and paginate database entries via `POST /databases/{id}/query`
7. **Add a comment** — Post a comment to a page via `POST /comments`
8. **Archive / delete a page** — Move a page to trash via `PATCH /pages/{id}` with `archived: true`

---

## 2. Trigger Phrases

Activate this skill when the user says (or implies) any of the following:

1. "search Notion for [query]"
2. "find a Notion page about [topic]"
3. "get the content of this Notion page"
4. "read a Notion page"
5. "create a new page in Notion"
6. "add a new entry to my Notion database"
7. "query my Notion task database"
8. "update Notion page properties"
9. "add a comment to that Notion page"
10. "archive the Notion page [title]"
11. "create a Notion database"
12. "sync data to Notion"
13. "Notion integration setup"
14. "use the Notion API"
15. "Notion page [title]"

---

## 3. Prerequisites

### 3.1 Notion Integration Token (Internal Integration)

1. Go to **https://www.notion.so/profile/integrations**
2. Click **"New integration"**
3. Fill in:
   - **Name:** a descriptive label (e.g., "Encre Bot")
   - **Associated workspace:** select your workspace
   - **Type:** Internal
4. Click **Submit**
5. Copy the **Internal Integration Token** — it starts with `secret_`

### 3.2 Share Pages / Databases with the Integration

By default, an integration can only see content it has been explicitly given access to.

1. Open the target page or database in Notion
2. Click the **`···`** (three-dot) menu in the top-right corner
3. Select **"Add connections"** or **"Connect to"**
4. Find and enable your integration by name
5. The integration can now access that page (and its children if appropriate)

### 3.3 Required Configuration

Store the token as an environment variable:

```shell
export NOTION_TOKEN="secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

Or set it in the Encre gateway config under `plugins.entries.notion.config.token`.

---

## 4. Detailed Steps — API Reference

**Base URL:** `https://api.notion.com/v1`  
**Auth:** `Authorization: Bearer {NOTION_TOKEN}`  
**Notion-Version:** `2022-06-28` (required in all request headers)

All requests use JSON. Errors follow the Notion error format.

---

### 4.1 Search Pages / Databases

**Endpoint:** `POST https://api.notion.com/v1/search`

**When to use:** The user wants to find pages or databases by keyword.

**Request headers:**

```
Authorization: Bearer {NOTION_TOKEN}
Notion-Version: 2022-06-28
Content-Type: application/json
```

**Request body:**

```json
{
  "query": "quarterly report",
  "filter": { "value": "page", "property": "object" },
  "sort": { "direction": "descending", "timestamp": "last_edited_time" },
  "page_size": 10
}
```

- `filter.value`: `"page"`, `"database"`, or omit entirely to search both
- `page_size`: max 100 per page

**Success response (200):**

```json
{
  "object": "list",
  "results": [
    {
      "object": "page",
      "id": "abcd1234-abcd-1234-abcd-1234abcd1234",
      "properties": {
        "title": {
          "title": [{ "type": "text", "text": { "content": "Q3 Quarterly Report" } }]
        }
      },
      "last_edited_time": "2024-11-01T12:00:00.000Z",
      "url": "https://www.notion.so/abcd1234abcd1234abcd1234abcd1234"
    }
  ],
  "has_more": false,
  "next_cursor": null
}
```

**Error response (401/403):**

```json
{
  "object": "error",
  "code": "unauthorized",
  "message": "Make sure the token is valid and the page is shared with your integration."
}
```

---

### 4.2 Get Page Content

**Endpoint:** `GET https://api.notion.com/v1/pages/{page_id}`

**When to use:** The user wants to read a specific page's metadata and properties.

**Page ID extraction:** The ID is the 32-character hex string (with hyphens) at the end of the Notion URL.

```
URL:  https://www.notion.so/Team/Project-Status-1234abcd1234abcd
ID:   1234abcd-1234-abcd-1234-abcd1234abcd1234
```

**Success response (200):** Returns page object with `properties`, `url`, `parent`, `created_time`, and `last_edited_time`.

**To retrieve the page's block content (the actual text):**

**Endpoint:** `GET https://api.notion.com/v1/blocks/{page_id}/children?page_size=100`

**Success response (200):**

```json
{
  "object": "list",
  "results": [
    {
      "id": "block-uuid-here",
      "type": "paragraph",
      "has_children": false,
      "paragraph": {
        "rich_text": [
          {
            "type": "text",
            "text": { "content": "Hello world", "link": null },
            "annotations": { "bold": false, "italic": false, "strikethrough": false, "underline": false, "code": false, "color": "default" }
          }
        ]
      }
    },
    {
      "id": "heading-block-uuid",
      "type": "heading_2",
      "heading_2": {
        "rich_text": [{ "type": "text", "text": { "content": "Section Title" } }]
      }
    }
  ],
  "has_more": false,
  "next_cursor": null
}
```

**Common block types:** `paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `toggle`, `code`, `quote`, `callout`, `divider`, `image`, `video`, `embed`, `bookmark`, `table`, `table_row`, `child_page`, `unsupported`

**To check if a block has children and fetch them recursively:**

```json
{
  "id": "parent-block-id",
  "type": "toggle",
  "has_children": true,
  ...
}
# Follow up with: GET /v1/blocks/{parent-block-id}/children
```

---

### 4.3 Create a Page

**Endpoint:** `POST https://api.notion.com/v1/pages`

**When to use:** The user wants to create a new Notion page.

**Option A — Create as a child of an existing page (sub-page):**

```json
{
  "parent": { "type": "page_id", "page_id": "parent-page-id" },
  "properties": {
    "title": {
      "title": [{ "type": "text", "text": { "content": "New Project Page" } }]
    }
  },
  "children": [
    {
      "object": "block",
      "type": "paragraph",
      "paragraph": {
        "rich_text": [{ "type": "text", "text": { "content": "Project kickoff notes." } }]
      }
    },
    {
      "object": "block",
      "type": "heading_2",
      "heading_2": {
        "rich_text": [{ "type": "text", "text": { "content": "Goals" } }]
      }
    }
  ]
}
```

**Option B — Create as a new entry in a database:**

```json
{
  "parent": { "type": "database_id", "database_id": "database-id" },
  "properties": {
    "Name": { "title": [{ "type": "text", "text": { "content": "New Task" } }] },
    "Status": { "select": { "name": "To Do" } },
    "Priority": { "select": { "name": "High" } },
    "Due Date": { "date": { "start": "2024-12-31" } },
    "Estimate": { "number": 3 }
  }
}
```

**Success response (200):** Returns the created page object with `id` and `url`.

**Error (400) — missing title:**

```json
{
  "object": "error",
  "code"

> *This skill was truncated from its original 21282 chars. For the full version, use `web_search`/`web_fetch` to find the latest documentation.*