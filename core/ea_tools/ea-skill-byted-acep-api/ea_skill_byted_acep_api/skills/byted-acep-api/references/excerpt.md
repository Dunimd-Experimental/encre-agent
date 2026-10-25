
## Screenshot, File, and Command Operations

Screenshot:

```shell
python3 scripts/vephone_cli.py get-presigned-edge-url <product_id> <pod_id> \
  --api-type TakeScreenshot \
  --payload RoundId=<unique_round_id> \
  --payload MimeType=png \
  --timeout 5 \
  --ttl 60

python3 scripts/vephone_cli.py batch-screen-shot <product_id> <pod_id>
```

For regular screenshots, prefer `get-presigned-edge-url --api-type TakeScreenshot`. Both `product_id` and `pod_id` are positional parameters; if the user just wants to see the screenshot result, prefer the direct screenshot path. `batch-screen-shot` is retained as a fallback, suitable for scenarios where the project has screenshot storage configured and explicitly needs this capability.

Direct Edge URL:

```shell
python3 scripts/vephone_cli.py get-presigned-edge-url <product_id> <pod_id> \
  --api-type TakeScreenshot \
  --payload RoundId=<unique_round_id> \
  --payload MimeType=png \
  --timeout 5 \
  --ttl 60
```

`get-presigned-edge-url` is the preferred path for screenshots and direct access, with regular screenshots preferring `--api-type TakeScreenshot`. Signed URLs are sensitive information; do not output directly unless explicitly requested by the user. There are many combinations of `--api-type`, `--api-path`, and `--payload` for different scenarios; directly check `python3 scripts/vephone_cli.py get-presigned-edge-url -h`.

If the user explicitly requests sandbox access, the following commands can be used:

```shell
python3 scripts/vephone_cli.py get-presigned-edge-url <product_id> <pod_id> --api-type Sandbox --api-path /sandbox/ws
python3 scripts/vephone_cli.py get-presigned-edge-url <product_id> <pod_id> --api-type Sandbox --api-path /sandbox/exec
python3 scripts/vephone_cli.py get-presigned-edge-url <product_id> <pod_id> --api-type Sandbox --api-path /sandbox/healthz
```

Do not perform sandbox direct access tests unless explicitly requested by the user.

Screen Recording:

```shell
python3 scripts/vephone_cli.py start-recording <product_id> <pod_id> --duration-limit 60 --round-id <unique_round_id>
python3 scripts/vephone_cli.py stop-recording <product_id> <pod_id>
```

File:

```shell
python3 scripts/vephone_cli.py push-file <product_id> <pod_id> ./app.apk /sdcard/Download/app.apk --overwrite
python3 scripts/vephone_cli.py pull-file <product_id> <pod_id> /sdcard/Download/file.txt --output ./file.txt
```

`push-file` is used to upload local files, `pull-file` is used to download files from within the instance to a local path.

Command Execution:

```shell
python3 scripts/vephone_cli.py run-command <product_id> <pod_id> "cmd"
python3 scripts/vephone_cli.py run-sync-command <product_id> <pod_id> "cmd" --permission-type root --timeout-second 10 --result-length 10240
```

Use harmless commands for diagnostics (`ls`, `echo`, `pm list packages`). Commands that modify files, install packages, change settings, or start/stop applications are all state-changing operations.

For focused Android input fields, you can input Chinese and English via IME commands:

```shell
python3 scripts/vephone_cli.py run-sync-command <product_id> <pod_id> 'ime inject_text "你好 (hello)"'
python3 scripts/vephone_cli.py run-sync-command <product_id> <pod_id> 'ime inject_text "hello world"'
python3 scripts/vephone_cli.py run-sync-command <product_id> <pod_id> 'ime clear_input_text'
```

To replace existing content, first use `ime clear_input_text`, then `ime inject_text`. These commands require the target input field to already be focused, usually requiring a click via `input tap x y` first.

`pull-logcat` is used to pull instance logs. Supports output path, resume download, and sharding parameters; for specific parameters, directly check `python3 scripts/vephone_cli.py pull-logcat -h`.

```shell
python3 scripts/vephone_cli.py pull-logcat <product_id> <pod_id> --output ~/Downloads/logcat_<pod_id>
python3 scripts/vephone_cli.py pull-logcat <product_id> <pod_id> --chunk-size 800 --concurrency 5 --retries 3
python3 scripts/vephone_cli.py pull-logcat <product_id> <pod_id> --resume
```

For routine log export, simply use the most basic `pull-logcat` command.

## Task, Layout, Tag, and Network Query

Tasks:

```shell
python3 scripts/vephone_cli.py list-tasks <product_id>
python3 scripts/vephone_cli.py get-task-info <product_id> <task_id>
```

Screen Layout:

```shell
python3 scripts/vephone_cli.py list-display-layouts <product_id>
python3 scripts/vephone_cli.py detail-display-layout <product_id> <display_layout_id>
```

Tags:

```shell
python3 scripts/vephone_cli.py list-tags <product_id>
python3 scripts/vephone_cli.py create-tag <product_id> --tag-name <tag_name> --tag-desc <tag_desc>
python3 scripts/vephone_cli.py update-tag <product_id> --tag-id <tag_id> --tag-name <tag_name> --tag-desc <tag_desc>
python3 scripts/vephone_cli.py delete-tag <product_id> --tag-id-list <tag_id1,tag_id2>
python3 scripts/vephone_cli.py attach-tag <product_id> --tag-id <tag_id> --pod-id-list <pod_id1,pod_id2>
```

Network Configuration:

```shell
python3 scripts/vephone_cli.py list-port-mapping-rules <product_id>
python3 scripts/vephone_cli.py detail-port-mapping-rule <product_id> <port_mapping_rule_id>
python3 scripts/vephone_cli.py list-dns-rules <product_id>
python3 scripts/vephone_cli.py detail-dns-rule <product_id> <dns_id>
python3 scripts/vephone_cli.py list-custom-routes <product_id>
```
