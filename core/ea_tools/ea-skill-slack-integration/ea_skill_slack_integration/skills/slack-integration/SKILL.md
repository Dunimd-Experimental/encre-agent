---
name: slack-integration
description: Slack integration - messaging, channel management, conversation search
user_invocable: true
hidden: true
context: inline
---

## Slack Integration
# SKILL.md — Slack Integration for Encre

---

## 1. Metadata

**Name:** `slack-integration`  
**Description:** Send messages, list channels, query users, upload files, and manage scheduled messages in Slack via the Web API. All in English.  
**Trigger Phrases:** See Section 2.  
**Capabilities:** See Section 3.  
**Prerequisites:** See Section 4.  
**Output Format:** See Section 6.  
**Caveats:** See Section 9.

---

## 2. Trigger Phrases

The following natural English phrases activate this skill. The user does not need to use the exact wording — any reasonable paraphrase of these intents will trigger it.

1. "Send a message to #general in Slack"
2. "Post a message to the engineering channel"
3. "Message someone on Slack by email"
4. "Send a direct message to John on Slack"
5. "Upload this file to Slack"
6. "Post a message to Slack with a block kit attachment"
7. "List all channels in our Slack workspace"
8. "What channels do we have in Slack?"
9. "Find a user on Slack by name"
10. "Look up a Slack user by their email address"
11. "Who is this Slack user? — @sarah.chen"
12. "Schedule a message in Slack for tomorrow at 9am"
13. "Create a scheduled Slack message"
14. "Delete a scheduled Slack message"
15. "Search messages in Slack"
16. "Find messages in Slack about the deployment"
17. "Set my Slack status to DND"
18. "What's the Slack user ID for someone?"

---

## 3. Capabilities

This skill is modular. Each capability maps to one or more Slack Web API methods. The skill performs only the capability requested; it never combines unrelated operations.

### 3.1 Send a Plain Text Message

**Slack API:** `POST https://slack.com/api/chat.postMessage`  
**Auth:** Bot Token (`xoxb-...`)  
**Scope required:** `chat:write`

Send a plain-text message to a public channel, private channel, or DM. The target can be specified by channel name (`#general`), channel ID (`C0123456789`), or user ID (`U0123456789`) for DMs.

**Required arguments:**
- `channel` — channel name, ID, or user ID
- `text` — message text (max 40,000 characters)

**Optional arguments:**
- `username` — display name override for the bot
- `icon_emoji` — emoji icon (e.g., `:robot_face:`)
- `thread_ts` — to reply in a thread, pass the parent message's `ts`

### 3.2 Send a Formatted Block Kit Message

**Slack API:** `POST https://slack.com/api/chat.postMessage`  
**Auth:** Bot Token  
**Scope required:** `chat:write`

Send a richly formatted message using Slack Block Kit (sections, dividers, buttons, images, etc.). Pass a JSON array of blocks as the `blocks` argument. The skill will validate the block structure before sending.

**Required arguments:**
- `channel` — channel name, ID, or user ID
- `blocks` — JSON array of Block Kit objects

**Optional arguments:**
- `text` — plain-text fallback (required by Slack if using blocks, used in notifications/previews)

### 3.3 List Channels / Conversations

**Slack API:** `GET https://slack.com/api/conversations.list`  
**Auth:** Bot Token  
**Scope required:** `channels:read` (public), `groups:read` (private), `im:read` (DMs), `mpim:read` (MPIMs)

Retrieve a paginated list of all channels the bot has access to. Supports filtering by type (`public_channel`, `private_channel`, `im`, `mpim`).

**Optional arguments:**
- `types` — comma-separated list: `public_channel,private_channel,im,mpim`
- `limit` — results per page (default 200, max 1000)
- `cursor` — pagination cursor from a previous response

**Returns:** An array of channel objects with `id`, `name`, `is_private`, `num_members`, `topic`, `purpose`.

### 3.4 Get User Info

**Slack API:** `GET https://slack.com/api/users.info`  
**Auth:** Bot Token  
**Scope required:** `users:read`

Look up a Slack user by their user ID. Returns profile, status, timezone, and workspace info.

**Required arguments:**
- `user` — Slack user ID (e.g., `U0123456789`)

**Alternative lookup by email:**
1. `GET https://slack.com/api/users.lookupByEmail?email=<email>`  
   Scope required: `users:read`

**Returns:** User object with `id`, `name`, `real_name`, `profile` (avatar, title, email, phone), `status`, `tz`.

### 3.5 Upload a File

**Slack API:** `POST https://slack.com/api/files.uploadV2`  
**Auth:** Bot Token  
**Scope required:** `files:write`

Upload a file to a Slack channel or DM. The file can be referenced by local path (the skill will read and upload it) or by URL.

**Required arguments:**
- `filename` — name of the file to display in Slack
- `channel` — target channel or user ID
- `file` — local file path or HTTPS URL

**Optional arguments:**
- `title` — file title
- `initial_comment` — comment to include with the file
- `filetype` — MIME type hint (e.g., `pdf`, `png`, `json`)

**Note:** Files are uploaded in a single request with `multipart/form-data`. Maximum file size is 1 GB for paid workspaces, 256 MB for free.

### 3.6 Scheduled Messages

**Slack APIs:**
- `POST https://slack.com/api/chat.scheduleMessage` — create
- `GET https://slack.com/api/chat.scheduledMessages.list` — list
- `DELETE https://slack.com/api/chat.deleteScheduledMessage` — delete

**Auth:** Bot Token  
**Scopes required:** `chat:write` (post), `chat:write` (list/delete)

Create a message to be delivered at a specified time. Delete or list pending scheduled messages.

**Create required arguments:**
- `channel` — target channel
- `text` — message text
- `post_at` — Unix timestamp or ISO 8601 datetime string for delivery time

**Create optional arguments:**
- `blocks` — Block Kit JSON array
- `thread_ts` — reply in thread

**List required arguments:** none (returns all scheduled messages for the app)  
**List optional arguments:**
- `channel` — filter to a specific channel
- `limit` — max results (default 100)

**Delete required arguments:**
- `channel` — channel ID where the scheduled message will post
- `scheduled_message_id` — ID returned from scheduleMessage

### 3.7 Search Messages

**Slack API:** `GET https://slack.com/api/search.messages`  
**Auth:** Bot Token  
**Scope required:** `search:read`

Search message history across all visible channels.

**Required arguments:**
- `query` — search query string (supports Slack search operators like `from:@user`, `in:#channel`, `has:file`, `on:2024-01-15`)

**Optional arguments:**
- `count` — results per page (default 100, max 100)
- `page` — page number
- `sort` — `score` (default) or `timestamp`
- `sort_dir` — `asc` or `desc`

**Returns:** Matches with message text, channel, user, timestamp, and highlights.

### 3.8 Set User Status (Bot Own Status)

**Slack API:** `POST https://slack.com/api/users.profile.set`  
**Auth:** Bot Token  
**Scope required:** `users.profile:write`

Set the bot user's custom status emoji and status text. Also used to set away/dnd status via `users.setPresence`.

**Required arguments for status:**
- `profile` — JSON object with `status_emoji` and `status_text`

**Required arguments for presence:**
- `presence` — `auto` (active) or `away` (away)

---

## 4. Prerequisites

### 4.1 Slack App & Bot Token

You must create a Slack App with a Bot Token before using this skill.

1. Go to **https://api.slack.com/apps** and click **Create New App** → **From scratch**.
2. Give the app a name (e.g., "Encre Integration"), pick your workspace, and click **Create App**.
3. Under **Features**, click **OAuth & Permissions** in the left sidebar.
4. Scroll to **Bot Token Scopes** and add these scopes depending on which capabilities you need:

| Capability | Required Scopes |
|---|---|
| Send plain message | `chat:write` |
| Send Block Kit message | `chat:write` |
| List channels | `channels:read`, `groups:read`, `im:read`, `mpim:read` |
| Get user info | `users:read` |
| Lookup by email | `users:read` |
| Upload file | `files:write` |
| Scheduled messages | `chat:write` |
| Search messages | `search:read` |
| Set status | `users.profile:write`, `users:write` |

5. Click **Install to Workspace** → **Allow** to generate a Bot Token (format: `xoxb-...`)
6. Copy a

> *This skill was truncated from its original 17264 chars. For the full version, use `web_search`/`web_fetch` to find the latest documentation.*