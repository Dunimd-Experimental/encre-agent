---
name: gmail-2
description: Gmail email access via MorphixAI proxy
user_invocable: true
hidden: true
context: inline
---

## Gmail 2
# Gmail (Currently Unavailable)

> **Status: Currently Unavailable** — Gmail account not yet linked, this tool is temporarily unavailable. To enable, link your Gmail account via the `mx_link` tool (app: `gmail`).

Manage Gmail inbox via the `mx_gmail` tool: read, search, send emails, manage labels.

## Prerequisites

1. **Install plugin**: `Encre plugins install Encre-morphixai`
2. **Get API Key**: Visit [morphix.app/api-keys](https://morphix.app/api-keys) to generate a `mk_xxxxxx` key
3. **Set environment variable**: `export MORPHIXAI_API_KEY="mk_your_key_here"`
4. **Link account**: Visit [morphix.app/connections](https://morphix.app/connections) to link your Gmail account, or link via the `mx_link` tool (app: `gmail`)

## Core Operations

### View User Info

```
mx_gmail:
  action: get_profile
```

### List Emails

```
mx_gmail:
  action: list_messages
  max_results: 10
```

> `list_messages` only returns a list of email IDs; use `get_message` to retrieve full content.

### View Email Details

```
mx_gmail:
  action: get_message
  message_id: "18dxxxxxxxx"
```

### Search Emails

```
mx_gmail:
  action: search_messages
  query: "from:boss@company.com subject:周报"
  max_results: 5
```

> Gmail search syntax supports:
> - `from:` / `to:` — Sender / Recipient
> - `subject:` — Subject
> - `is:unread` / `is:starred` — Unread / Starred
> - `newer_than:7d` / `older_than:30d` — Time range
> - `has:attachment` — Has attachments
> - `label:` — Label

### Send Email

```
mx_gmail:
  action: send_mail
  to: "colleague@company.com"
  subject: "项目更新"
  body: "Hi，项目进展如下：\n1. 完成了 API 开发\n2. 正在编写测试"
  cc: "manager@company.com"
```

### List Labels

```
mx_gmail:
  action: list_labels
```

### Mark as Read

```
mx_gmail:
  action: mark_as_read
  message_id: "18dxxxxxxxx"
```

### Delete Email (Move to Trash)

```
mx_gmail:
  action: trash_message
  message_id: "18dxxxxxxxx"
```

## Common Workflows

### View Unread Emails

```
1. mx_gmail: list_messages, q: "is:unread", max_results: 5
2. mx_gmail: get_message, message_id: "xxx"  → view one by one
3. mx_gmail: mark_as_read, message_id: "xxx"  → mark as read
```

### Search and Reply (by Sending New Email)

```
1. mx_gmail: search_messages, query: "from:client@example.com"
2. mx_gmail: get_message → view content
3. mx_gmail: send_mail, to: "client@example.com", subject: "Re: xxx"
```

## Notes

- `list_messages` returns a list of email IDs; use `get_message` to retrieve full content
- Sent email content is plain text
- Gmail search syntax is powerful, make full use of it
- The `account_id` parameter is usually omitted; the tool auto-detects the linked Gmail account
