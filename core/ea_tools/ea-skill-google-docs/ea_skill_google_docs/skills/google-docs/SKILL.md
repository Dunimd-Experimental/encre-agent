---
name: google-docs
description: "Google Docs API integration with managed OAuth. Create documents, insert text, apply formatting, and manage content."
aliases: [google-docs, google-docs-api]
argument_hint: "What you want to do with Google Docs"
user_invocable: true
hidden: true
---

# google-docs
# Google Docs

Access the Google Docs API with managed OAuth authentication. Create documents, insert and format text, and manage document content.

## Quick Start

**CLI:**

```shell
maton google-docs document view DOC_ID
```

```shell
maton api '/google-docs/v1/documents/{documentId}'
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/google-docs/v1/documents/{documentId}')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

## Base URL

```
https://api.maton.ai/google-docs/{native-api-path}
```

Maton proxies requests to `docs.googleapis.com` and automatically injects your OAuth token.

## Installation

**NPM:**
```shell
npm install -g @maton/cli
```

**Homebrew:**
```shell
`terminal brew install` maton-ai/cli/maton
```

## Authentication

**CLI:**

```shell
maton login                          # Opens browser for API key
maton login --interactive            # Skip browser, paste API key directly
maton whoami                         # Show current auth state
```

**Manual:**

1. Sign in or create an account at [maton.ai](https://maton.ai)
2. Go to [maton.ai/settings](https://maton.ai/settings)
3. Copy your API key
4. Set your API key as `MATON_API_KEY`:

```shell
export MATON_API_KEY="YOUR_API_KEY"
```

## Connection Management

Manage your Google OAuth connections at `https://api.maton.ai`.

### List Connections

**CLI:**

```shell
maton connection list google-docs --status ACTIVE
```

```shell
maton api -X GET /connections -f app=google-docs -f status=ACTIVE
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/connections?app=google-docs&status=ACTIVE')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

### Create Connection

**CLI:**

```shell
maton connection create google-docs
```

```shell
maton api /connections -f app=google-docs
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
data = json.dumps({'app': 'google-docs'}).encode()
req = urllib.request.Request('https://api.maton.ai/connections', data=data, method='POST')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
req.add_header('Content-Type', 'application/json')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

### Get Connection

**CLI:**

```shell
maton connection view {connection_id}
```

```shell
maton api /connections/{connection_id}
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/connections/{connection_id}')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

**Response:**
```json
{
  "connection": {
    "connection_id": "{connection_id}",
    "status": "ACTIVE",
    "creation_time": "2025-12-08T07:20:53.488460Z",
    "last_updated_time": "2026-01-31T20:03:32.593153Z",
    "url": "https://connect.maton.ai/?session_token=...",
    "app": "google-docs",
    "metadata": {}
  }
}
```

Open the returned `url` in a browser to complete OAuth authorization.

### Delete Connection

**CLI:**

```shell
maton connection delete {connection_id}
```

```shell
maton api -X DELETE /connections/{connection_id}
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/connections/{connection_id}', method='DELETE')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

### Specifying Connection

If you have multiple Google Docs connections, specify which one to use:

**CLI:**

```shell
maton google-docs document view DOC_ID --connection {connection_id}
```

```shell
maton api /google-docs/v1/documents/{documentId} --connection {connection_id}
```

**Python:**

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/google-docs/v1/documents/{documentId}')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
req.add_header('Maton-Connection', '{connection_id}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

If you have multiple connections, always specify the connection to ensure requests go to the intended account.

## Security & Permissions

- Access is scoped to documents, content, formatting, and document metadata within the connected Google Docs account.
- **All write operations require explicit user approval.** Before executing any create, update, or delete call, confirm the target resource and intended effect with the user.

## API Reference

### Get Document

```shell
GET /google-docs/v1/documents/{documentId}
```

Example:

```shell
maton google-docs document view DOC_ID            # human-readable summary (id, title, revision)
maton google-docs document view DOC_ID --json     # full document payload (body, styles, etc.)
```

### Create Document

```shell
POST /google-docs/v1/documents
Content-Type: application/json

{
  "title": "New Document"
}
```

Example:

```shell
maton google-docs document create --title 'New Document'
```

### Batch Update Document

```shell
POST /google-docs/v1/documents/{documentId}:batchUpdate
Content-Type: application/json

{
  "requests": [
    {
      "insertText": {
        "location": {"index": 1},
        "text": "Hello, World!"
      }
    }
  ]
}
```

Example:

```shell
maton google-docs document write DOC_ID --text 'Hello, World!'
```

## Common batchUpdate Requests

### Insert Text

```json
{
  "insertText": {
    "location": {"index": 1},
    "text": "Text to insert"
  }
}
```

### Delete Content

```json
{
  "deleteContentRange": {
    "range": {
      "startIndex": 1,
      "endIndex": 10
    }
  }
}
```

### Replace All Text

```json
{
  "replaceAllText": {
    "containsText": {
      "text": "{{placeholder}}",
      "matchCase": true
    },
    "replaceText": "replacement value"
  }
}
```

### Insert Table

```json
{
  "insertTable": {
    "location": {"index": 1},
    "rows": 3,
    "columns": 3
  }
}
```

### Update Text Style

```json
{
  "updateTextStyle": {
    "range": {
      "startIndex": 1,
      "endIndex": 10
    },
    "textStyle": {
      "bold": true,
      "fontSize": {"magnitude": 14, "unit": "PT"}
    },
    "fields": "bold,fontSize"
  }
}
```

### Insert Page Break

```json
{
  "insertPageBreak": {
    "location": {"index": 1}
  }
}
```

## Code Examples

### CLI

```shell
# Create a new document
maton google-docs document create --title 'Sprint notes'

# View a document
maton google-docs document view DOC_ID

# Write text to a document
maton google-docs document write DOC_ID --text 'Hello!'
```

### JavaScript

```javascript
// Create document
const response = await fetch(
  'https://api.maton.ai/google-docs/v1/documents',
  {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${process.env.MATON_API_KEY}`
    },
    body: JSON.stringify({ title: 'New Document' })
  }
);

// Insert text
await fetch(
  `https://api.maton.ai/google-docs/v1/documents/${docId}:batchUpdate`,
  {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${process.env.MATON_API_KEY}`
    },
    body: JSON.stringify({
      requests: [{ insertText: { location: { index: 1 }, text: 'Hello!' } }]
    })
  }
);
```

### Python

```python
import os
import requests

# Create document
response = requests.post(
    'https://api.maton.ai/google-docs/v1/documents',
    headers={'Authorization': f'Bearer {os.environ["MATON_API_KEY"]}'},
    json={'title': 'New Document'}
)
```

## Notes

- Index positions are 1-based (document starts at index 1)
- Use `endOfSegmentLocation` to append at end
- Multiple requests in batchUpdate are applied atomically
- Get document first to find correct indices for updates
- The `fields` parameter in style updates uses field mask syntax
- IMPORTANT: When using curl commands, use `rest_client -g` when URLs contain brackets (`fields[]`, `sort[]`, `records[]`) to disable glob parsing
- IMPORTANT: When piping curl output to `jq` or other commands, environment variables like `$MATON_API_KEY` may not expand correctly in some shell environments. You may get "Invalid API key" errors when piping.

## Error Handling

| Status | Meaning |
|--------|---------|
| 400 | Missing Google Docs connection |
| 401 | Invalid or missing Maton API key |
| 429 | Rate limited (10 req/sec per account) |
| 4xx/5xx | Passthrough error from Google Docs API |

### Troubleshooting: API Key Issues

**CLI:**

1. Check your auth state:

```shell
maton whoami
```

2. Verify the API key is valid by listing connections:

```shell
maton connection list
```

**Manual:**

1. Check that the `MATON_API_KEY` environment variable is set:

```shell
echo $MATON_API_KEY
```

2. Verify the API key is valid by listing connections:

```shell
python <<'EOF'
import urllib.request, os, json
req = urllib.request.Request('https://api.maton.ai/connections')
req.add_header('Authorization', f'Bearer {os.environ["MATON_API_KEY"]}')
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

### Troubleshooting: Invalid App Name

1. Ensure your URL path starts with `google-docs`. For example:

- Correct: `https://api.maton.ai/google-docs/v1/documents/{documentId}`
- Incorrect: `https://api.maton.ai/docs/v1/documents/{documentId}`

## Resources

- [Docs API Overview](https://developers.google.com/docs/api/how-tos/overview)
- [Get Document](https://developers.google.com/docs/api/reference/rest/v1/documents/get)
- [Create Document](https://developers.google.com/docs/api/reference/rest/v1/documents/create)
- [Batch Update](https://developers.google.com/docs/api/reference/rest/v1/documents/batchUpdate)
- [Request Types](https://developers.google.com/docs/api/reference/rest/v1/documents/request)
- [Document Structure](https://developers.google.com/docs/api/concepts/structure)
- [Maton CLI Manual](https://cli.maton.ai/manual)
- [Maton Community](https://discord.com/invite/dBfFAcefs2)
- [Maton Support](mailto:support@maton.ai)