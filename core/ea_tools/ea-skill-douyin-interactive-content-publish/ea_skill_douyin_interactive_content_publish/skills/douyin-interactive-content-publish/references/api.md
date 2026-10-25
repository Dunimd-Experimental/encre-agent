# MCP Tool Reference

## Service Overview

- **MCP Service Name**: `interative_content_mcp`
- **Transport Protocol**: Streamable HTTP
- **Auth**: OAuth 2.1 (handled automatically by IDE, no manual token configuration needed)

---

## Tool List

### 1. get_upload_token

Get a one-time upload credential (valid for 5 minutes) for uploading files via curl.

**Parameters:** None

**Response Fields:**

| Field | Description |
|-------|-------------|
| `upload_token` | One-time upload credential |
| `upload_url` | Upload endpoint URL |
| `expires_in` | Validity period (seconds) |

**Upload Flow:**
1. Call `get_upload_token`
2. Upload file via curl:
   ```shell
   curl -X POST '<upload_url>' \
     -H 'Authorization: UploadToken <upload_token>' \
     -F 'file=@<file path>'
   ```
3. Extract `data.uri` from the response JSON

> upload_token is one-time only; call multiple times for multiple files.

**Supported File Types:** zip, jpg, jpeg, png, gif, webp, bmp, svg, ico

**Upload Response Example:**
```json
{
  "code": 0,
  "data": {
    "uri": "tos-cn-i-xxx/game_v1.0.zip",
    "file_type": "zip",
    "file_name": "game_v1.0.zip"
  }
}
```

---

### 2. modify_game_app

Create or update an Interactive Space.

**Parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | Yes | 1-create, 2-update |
| `name` | Yes | Interactive Space name |
| `icon_uri` | Yes | Icon URI (obtained via upload) |
| `screen_direction` | Yes | 1-portrait, 2-landscape |
| `package_uri` | Yes | Zip package URI (obtained via upload) |
| `biz_id` | No | Business type, fixed to 3 |
| `biz_platform_type` | No | Platform type, fixed to 1 (Douyin) |
| `app_id` | Required for update | Required when updating |
| `desc` | No | Interactive Space description |
| `package_type` | No | Package type, fixed to 1 (Zip) |
| `package_desc` | No | Package source description, max 20 chars (current AI tool name or empty string) |

**Response:** Contains `AppID`, used for subsequent updates and reviews.

---

### 3. submit_audit_game_app

Submit an Interactive Space for review.

**Parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `biz_id` | Yes | Fixed to 3 |
| `biz_platform_type` | Yes | Fixed to 1 |
| `app_id` | Yes | AppID to submit for review |

---

### 4. query_game_app_list

Query the Interactive Space list.

**Parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `biz_id` | No | Business type, fixed to 3 |
| `biz_platform_type` | No | Platform type, fixed to 1 |
| `app_id` | No | Exact query by AppID |
| `search_key` | No | Fuzzy search keyword |
| `status` | No | Status filter (array) |
| `page_num` | No | Page number, starting from 1 |
| `page_size` | No | Items per page, default 20 |

**Status Codes:**

| Value | Meaning |
|-------|---------|
| 1 | Draft |
| 2 | Under Review |
| 3 | Review Rejected |
| 4 | Published |
| 5 | Unpublished |
