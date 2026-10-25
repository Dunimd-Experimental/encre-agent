---
name: byted-las-video-edit
description: "Smart video editing: Use natural language to describe segments/people/events to keep, automatically extract clips from long videos (reference images can be used for person/object matching). Triggered when users want to find segments by description, extract highlights, or perform smart editing."
  lasCliVersion: "0.3.8"
  requires:
    bins:
      - las-cli
  cliHelp: "las-cli operators help las_video_edit"
  operatorId: "las_video_edit"
  openclaw:
    identity:
      - type: tip
        env:
          - VE_TIP_TOKEN
        required: true
---

# Smart Video Editing (`las_video_edit`)

Use this Skill when the user wants to edit, trim, or search video content through natural language descriptions.

## Notes (⚠️ Must Follow)
- Never ask the user to input AccessKey and SecretKey.
- File download must only use the `las-cli tos download` command (compatible with both tos and non-tos resources).
- The output of `las-cli operators call` and `las-cli price` commands is very important and must be returned to the user in full.

## Workflow (Strictly Follow Steps)

Copy this checklist and track progress:

```text
Execution Progress:
- [ ] Step 0: Prerequisite Check
- [ ] Step 1: Initialization and Preparation
- [ ] Step 2: Price Estimation
- [ ] Step 3: Execute Task and Present Results
- [ ] Step 4: Troubleshooting
```

### Step 0: Prerequisite Check

- **Version check (required)**: Run las-cli install 0.3.8. If the las-cli command does not exist, run npm i -g "@volcengine/las-cli@0.3.8"
- **New version reminder**: Run `npm info @volcengine/las-cli version` to check the latest available Skill version (`las-cli` corresponds one-to-one with the Skill version). If the returned version is higher than the version specified in this Skill, prompt the user to upgrade the current Skill. Upgrade method: ask the user to send the natural language command `byted-las-video-edit` (install the byted-las-video-edit skill for me), and wait for the automatic installation to complete.
- **Authentication check**: Prioritize using the runtime auto-injected platform identity; if the current environment has not auto-injected, run `las-cli config show` to confirm that the legacy credentials `las.apiKey` and `volcengine.region` are configured.

### Step 1: Initialization and Preparation

- **Upload resource**: If the input is a local video, upload it to TOS first.
- **Prepare parameters**: Create `params.json`, containing input, output, and editing instructions.
  ```json
  {
    "input_path": "tos://my-bucket/inputs/xxx.mp4",
    "output_path": "tos://my-bucket/outputs/video_edit/",
    "query": "find all clips containing cats (Find all clips containing cats)"
  }
  ```

### Step 2: Price Estimation (⚠️ Must Obtain User Confirmation)

The output of the `las-cli price` command is very important. Do not simplify, modify, or summarize it.

- **Calculate price**: Must return the full output of the following command (markdown format)
  ```shell
  las-cli price las_video_edit \
    --params-file ./params.json \
    --format markdown
  ```
- **User confirmation**: After outputting the estimated price, **must wait for user confirmation**.

### Step 3: Execute Task and Present Results (⚠️ Output Command Results Directly, Do Not Simplify)

The output of the `las-cli operators call` command is very important. Do not simplify, modify, or summarize it.

- **Execute**: Must return the full output of the following command (markdown format)
  ```shell
  las-cli operators call las_video_edit \
    --params-file ./params.json \
    --format markdown \
    --out ./result.md
  ```

### Step 4: Troubleshooting

Keep as is.

- **Check task status**: `las-cli task status <task_id>`.
- **Check input**: Verify that `input_path` is accessible.
- **Check configuration**: `las-cli config show`.
