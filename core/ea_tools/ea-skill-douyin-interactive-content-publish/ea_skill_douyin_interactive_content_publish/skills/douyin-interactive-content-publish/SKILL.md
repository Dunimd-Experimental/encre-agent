---
name: douyin-interactive-content-publish
description: "One-click publish tool for Interactive Space. Upload zip+icon to create/update interactive space apps."
aliases: [douyin-publish, interactive-space]
argument_hint: "What interactive app you want to publish"
user_invocable: true
hidden: true
---
context: inline
---

## Douyin Interactive Content Publish

Complete the full workflow from local zip package to publishing and going live for Interactive Space works. Core principle: minimize user input by automatically inferring information through zip package analysis, requiring only user confirmation.

Detailed API specifications at `references/api.md`.

### Interaction Principles

When asking the user a question or requesting input, always clearly specify where and how to input. Users may be in different IDE environments (e.g. Trae, Cursor, etc.) with different input methods:

- When providing multiple options, say "Reply with number 1/2/3 to select" or "Reply with the corresponding text to select"
- When needing the user to input a path, ID, or similar content, say "Please directly input xxx" or "Please paste the path into the input box"
- When a question has both preset options and allows custom input, say "Reply with the option number, or directly input custom content"
- Avoid vague open-ended questions; each question should provide a clear response format example

### MCP Server Information

- **Name**: `interative_content_mcp`
- **Transport**: Streamable HTTP
- **Auth**: Handled automatically by IDE (authorization page pops up on first connection)

### Execution Flow

> Strict sequential execution: Must start from Step 1 and complete progressively. Step 1 is a blocking prerequisite check; no subsequent steps may be executed until it passes.

#### Step 1: Check and Configure MCP (Blocking)

Do not proceed to any subsequent steps until this step passes.

> **Behavioral constraints (throughout the flow):**
> - Do NOT open browser, visit web pages, or guide the user to any platform/website for operations
> - Do NOT attempt to bypass MCP tools via HTTP requests, browser automation, or other means
> - All server interactions must be done exclusively through MCP tools
> - If MCP tools do not exist or are not loaded, proceed to 1.2 to auto-check and write MCP configuration; do not ask the user to manually configure or refresh
> - Only after auto-check/write and still unable to find MCP tools, prompt the user to open the MCP configuration page and click refresh
> - If MCP is unavailable due to auth, authorization, or login expiry, only prompt the user to open the current IDE/AI tool's configuration interface and complete auth/re-authorization for `interative_content_mcp` in MCP / MCP Servers / MCP settings; do not seek alternatives
> - If a tool call fails or returns an error, simply inform the user of the error; auth issues guide the user to the configuration interface, non-auth issues suggest refreshing MCP

##### 1.1 Check MCP Availability

Check whether tools provided by `interative_content_mcp` (`get_upload_token`, `modify_game_app`, `submit_audit_game_app`, `query_game_app_list`) exist in the available tools list.

- **All exist** → Call `query_game_app_list` (params `{"biz_id": 3, "biz_platform_type": 1, "page_num": 1, "page_size": 1}`) to probe:
  - Normal response → MCP available ✅, proceed to Step 2
  - Auth/authorization/login error → Prompt user to open the current IDE/AI tool's configuration interface, find `interative_content_mcp` in MCP / MCP Servers / MCP settings, and complete auth or re-authorization. Do NOT open browser, attempt other auth methods, or continue troubleshooting
- **Any tool missing** → MCP configuration may be missing or not yet loaded; proceed to 1.2 to auto-check and write configuration; do not ask the user to handle manually

##### 1.2 Auto-Check and Write MCP Configuration

Do not ask the user; complete automatically per the following flow.

**Determine config path:**

Run `uname -s` to determine the OS, then get the base config path for each IDE:

| OS | Base Path |
|----|-----------|
| Darwin (macOS) | `~/Library/Application Support/` |
| Linux | `~/.config/` |
| Windows (MINGW/MSYS/CYGWIN) | `$APPDATA/` |

Then check the following directories in order; **write if they exist**:

| IDE | Subdirectory |
|-----|-------------|
| Trae Solo (International) | `TRAE SOLO/User/` |
| Trae Solo (China) | `TRAE SOLO CN/User/` |
| Cursor | `Cursor/User/globalStorage/` |

Regardless of whether any of the above directories match, **additionally write** to the project root `.trae/mcp.json` as a fallback.

**Write logic (Python):**

Before writing, check if Python 3 is available (`python3 --version`). If not, help the user install it:

- macOS: Run `xcode-select --install` (system includes Python 3)
- Linux (Debian/Ubuntu): `sudo apt-get install -y python3`; (CentOS/RHEL): `sudo yum install -y python3`
- Windows: `winget install Python.Python.3.12`, or prompt the user to download from python.org

After installation, re-check `python3 --version` to confirm availability.

Once Python 3 is confirmed available, for each matched path, save the following Python script as a temporary file and execute it (replace `target_dir` with the actual path). If the config file does not exist, create and write `interative_content_mcp`; if the config file exists but does not have `interative_content_mcp`, append it; if `interative_content_mcp` already exists, keep it as-is and continue to the next path.

```python
import json, os, sys

target_dir = '<matched directory path>'
target_file = os.path.join(target_dir, 'mcp.json')

new_server = {
    'interative_content_mcp': {
        'url': 'https://vcreate.douyin.com/mgplatform/api/apps/interact_content/mcp',
        'oauth': {}
    }
}

os.makedirs(target_dir, exist_ok=True)

if os.path.exists(target_file):
    with open(target_file, 'r') as f:
        data = json.load(f)
    if 'interative_content_mcp' in data.get('mcpServers', {}):
        print(f'interative_content_mcp already exists, skipping: {target_file}')
        sys.exit(0)
    data.setdefault('mcpServers', {}).update(new_server)
else:
    data = {'mcpServers': new_server}

with open(target_file, 'w') as f:
    json.dump(data, f, indent=2)
print(f'Written: {target_file}')
```

##### 1.3 Verify After Write/Check

After writing or confirming the configuration exists, re-fetch the MCP tool list and check again whether tools from `interative_content_mcp` exist (same logic as 1.1).

- **Available** → Proceed to Step 2
- **Still not found** → Output the following message:

> MCP configuration has been checked/written, but the current session has not loaded the `interative_content_mcp` tool. Please operate in the current IDE/AI tool's MCP configuration page (do NOT open a browser or visit any website):
>
> 1. Open the **MCP** / **MCP Servers** / **MCP settings** entry for this tool
> 2. If you see `interative_content_mcp` in the list, click **Refresh / Reload / Reconnect** on the MCP configuration page
> 3. If you cannot find `interative_content_mcp` in the list, copy the JSON configuration below and manually add the server on the MCP configuration page, then click **Refresh / Reload / Reconnect**
> 4. If you see auth, authorization, login, or account configuration issues, complete auth or re-authorization on the configuration page, then click **Refresh / Reload / Reconnect** again
> 5. Come back and tell me "refreshed" and I will re-check
>
> ```json
> {
>   "mcpServers": {
>     "interative_content_mcp": {
>       "url": "https://vcreate.douyin.com/mgplatform/api/apps/interact_content/mcp"
>     }
>   }
> }
> ```

#### Step 2: Query Existing Works (Permission Check + Count Check)

Call `query_game_app_list` to query the user's current Interactive Space list:

```json
{"biz_id": 3, "biz_platform_type": 1, "page_num": 1, "page_size": 20}
```

**Determine based on response:**

**a) Success** → Record the returned `max_num` (maximum creatable count) and current work list, proceed to Step 3.

**b) Permission error** → User has not registered yet. First check if the error message contains a registration link:

- If error/message contains a URL (e.g. starting with `http://` or `https://`), prefer the link from the error message
- If no link in the error message, use the default registration link: `https://bytedance.larkoffice.com/share/base/form/shrcnEyRfMORxiHJj2BReaBF0Ys`

Then prompt:

> You do not have Interactive Space permissions yet. Please go to the registration page first:
> [Interactive Space Registration](link from error or default link)
>
> After registration and approval, proceed with the subsequent steps.

**c) Work count has reached the limit** → Cannot create new; ask the user to choose an action:

> Your Interactive Space count has reached the limit (<current count>/<max_num>). Unable to create new.
>
> You can choose (reply 1 or 2):
>
> 1. **Modify existing work** — Update content on an existing Interactive Space
> 2. **Delete old work** — Delete an old one then create new
>
> Please provide the AppID to operate on, or reply "help me check" to view your work list.

If the user replies "help me check" or does not know the AppID, display the queried work list as a table:

| AppID | Name | Status | Updated |
|-------|------|--------|---------|
| xxx | xxx | Draft | xxx |

After user selection:

- **Modify** → Record AppID; in Step 5 set `action` to 2 with `app_id`
- **Delete** → Prompt the user to manually delete from the management backend, then come back and re-run the query

#### Step 3: Collect Basic Information and Analyze Zip Package

Publishing Interactive Space requires two files: **zip package** and **icon file (300x300, jpg/png)**.

##### 3.1 Auto-Discover Files

Priority order:

1. Check if the user has already mentioned file paths in the conversation context
2. If not mentioned, search the current working directory and subdirectories:
   - zip package: `find . -maxdepth 2 -name "*.zip" -type f`
   - icon: `find . -maxdepth 2 \( -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" \) -type f`

After discovering candidate files, list them for user confirmation. If multiple files found, list all for user selection. If none found, directly ask the user to provide the path.

##### 3.2 Analyze Zip Package, Generate Suggestions

After obtaining the zip package, analyze its contents to automatically infer Interactive Space metadata:

1. **View package structure**: `unzip -l <zip file>` to list files
2. **Infer name**: From `index.html` `<title>` tag → project directory name → zip filename (priority descending)
3. **Generate description**: Read core files (`index.html`, `main.js`, `game.js`, etc.) and summarize gameplay in one sentence (under 50 characters)
4. **Infer screen orientation**: From canvas dimensions, viewport meta, etc. to infer landscape/portrait

Present the analysis results for user confirmation:

> I analyzed your zip package. Here are the suggested publishing details:
>
> - Package: `<zip filename>` (<file size>)
> - Structure: `<main file list>`
> - Name: `<inferred name>`
> - Description: `<generated description>`
> - Icon: `<user-provided icon filename>`
> - Orientation: `<portrait/landscape>`
> - Source: `<current AI tool name>` (auto-filled if recognizable, e.g. "Trae Solo", "Cursor"; leave blank if uncertain)
>
> Need to modify?
>
> - Confirm → Reply "confirm"
> - Modify → Directly input changes, e.g. "change name to xxx", "change description to xxx", or "change to landscape"

#### Step 4: Execute Upload

> **Important: Upload tokens are automatically obtained via the MCP tool `get_upload_token`. No manual operation required.**
>
> - `upload_token` and `upload_url` are one-time upload credentials for the local upload flow
> - They do NOT come from the Interactive Space platform, registration page, management backend, or any webpage
> - Do NOT ask the user to obtain, copy, or fill in `upload_token` / `upload_url` from any platform
> - Do NOT open a browser or visit any website to obtain upload credentials
> - The user only needs to confirm the zip package and icon file; the model will sequentially call MCP tools and execute curl uploads locally

After user confirms the information, start uploading files:

##### 4.1 Upload Zip Game Package

1. Call MCP tool `get_upload_token` to get a one-time credential for zip upload
2. Execute curl upload:
   ```shell
   curl -X POST '<upload_url>' \
     -H 'Authorization: UploadToken <upload_token>' \
     -F 'file=@<zip file path>'
   ```
3. Extract `data.uri` from the response as `package_uri`

##### 4.2 Upload Icon

1. Call MCP tool `get_upload_token` again to get a new one-time credential for icon upload (each token can only be used once)
2. Execute curl upload:
   ```shell
   curl -X POST '<upload_url>' \
     -H 'Authorization: UploadToken <upload_token>' \
     -F 'file=@<icon file path>'
   ```
3. Extract `data.uri` from the response as `icon_uri`

#### Step 5: Create/Update Interactive Space

Call MCP tool `modify_game_app`:

```json
{
  "action": 1,
  "biz_id": 3,
  "biz_platform_type": 1,
  "name": "<confirmed name>",
  "desc": "<confirmed description>",
  "icon_uri": "<icon URI from Step 4.2>",
  "screen_direction": 1,
  "package_uri": "<package URI from Step 4.1>",
  "package_type": 1,
  "package_desc": "<confirmed source>"
}
```

- New: `action` = 1
- Update: `action` = 2, include `app_id` (from Step 2 user selection)
- `package_desc`: Source description (max 20 chars), use the value confirmed in Step 3; if unchanged and empty, pass empty string `""`

Extract `AppID` from the response.

#### Step 6: Ask Whether to Submit for Review

After successful creation/update, ask the user:

> Interactive Space successfully created! AppID: <app_id>
>
> Submit for review now? (Reply 1 or 2)
>
> 1. Submit — Submit immediately, enter review process
> 2. No — Keep as draft, can submit manually later

**If user chooses to submit:**

Call `submit_audit_game_app`:

```json
{
  "biz_id": 3,
  "biz_platform_type": 1,
  "app_id": "<AppID>"
}
```

**If user chooses not to submit:**

Output completion info, informing the user they can submit later using `submit_audit_game_app`.

### Output Format

After the flow completes, output a summary table:

> Interactive Space publish flow complete

| Item | Content |
|------|---------|
| Game Package | `<zip filename>` → `package_uri` |
| Icon | `<icon filename>` → `icon_uri` |
| AppID | `<app_id>` |
| Name | `<name>` |
| Description | `<desc>` |
| Orientation | Portrait/Landscape |
| Status | `<status text>` |

> Next: Awaiting review results. You can query the latest status anytime using the AppID.

If not submitted for review, fill "Draft (not submitted)" for the Status row and append:

> Let me know when you need to submit for review.
