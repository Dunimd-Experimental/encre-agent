---
name: byted-acep-api
description: Manage and troubleshoot Volcano Engine cloud phone resources via local Python CLI and OpenAPI client. Suitable for querying instances and resources, taking screenshots, executing commands, viewing tasks, checking applications, host and data center capacity, tags, DNS, routing, and operating authorized test cloud phone instances.
---

# Volcano Engine Cloud Phone Skill

Use this skill when the user requests to view or operate Volcano Engine cloud phone resources under a configured test project.

## Runtime Constraints

- Do not statically configure DC. Select DC based on user request or current resource availability.
- First look up the project ID via `list-products`, then explicitly pass `product_id` in each command.
- When creating and ordering resources, explicitly select the business resource type: use `100` for cloud disk storage and `200` for local storage.
- When updating instance images or specifications, prefer shutting down first; when forced updates are involved, remind the user to confirm the potential impact.
- Post-paid instance resource groups only support re-purchase. Before ordering post-paid resources, confirm that the project has already placed an order in the Volcano Engine cloud phone console and that an `OrderId` can be found.
- For screenshots, prefer the direct connection method `get-presigned-edge-url --api-type TakeScreenshot`.
- `batch-screen-shot` is used as a fallback; use it only when the project has screenshot storage configured and the user explicitly needs this path.
- Do not output signed URLs unless explicitly requested by the user.
- When querying installed applications, prefer `get-pod-app-list`; fall back to Android `pm list packages` only when truly necessary.
- Do not output access key, secret key, Authorization header, or full signed URLs unless explicitly requested by the user.
- Create, delete, power on/off, reboot, reset, install/uninstall, file transfer, app start/stop, and Android command execution are all state-changing operations. Unless the user explicitly requests the operation, confirm first.

## Command Entry

The following examples assume execution in the current skill package directory:

```shell
python3 scripts/vephone_cli.py <command> [args]
```

This skill comes with `scripts/vephone_cli.py` and `scripts/core/` built-in, and can run directly without depending on the repository root directory's implementation.

Commands expose common parameters as explicit flags. Except for a few query commands like `list-products`, most business commands take `product_id` as the first positional parameter; to see which filter conditions a command supports, use `python3 scripts/vephone_cli.py <command> -h`. If the authentication configuration is not in the default location, you can specify the config file path with `--config /path/to/config.json`; this global parameter can be placed before or after the subcommand.

If existing commands do not yet cover the target OpenAPI Action, use the generic command:

```shell
python3 scripts/vephone_cli.py action-call ListOperableProduct --json-body --param Count=10 --param CloudphoneProductType=5
python3 scripts/vephone_cli.py action-call GetProductResource --product-id <product_id>
python3 scripts/vephone_cli.py action-call SetProxy --json-body --params-json '{"ProductId":"<product_id>","PodIdList":["pod-1"],"ProxyStatus":1,"ProxyConfig":{"version":"v2","type":"socks5","address":"203.0.113.52","port":"12345"}}'
```

The `--param` of `action-call` uses `Key=Value` format. `Value` is preferentially parsed as JSON, and treated as a string on failure, so you can directly pass `true`, `123`, `[1,2]`, `{"k":"v"}`. Note that in common shells, if you don't quote/escape `Key=Value`, the double quotes in something like `--param Foo={"k":"v"}` may be consumed by the shell, causing the program to actually receive `Foo={k:v}` and trigger JSON parsing failure. When `Value` contains double quotes, spaces, or complex JSON, wrap the entire `Key=Value` in single quotes (e.g., `--param 'Foo={"k":"v"}'` / `--param 'PodIdList=["pod-1"]'`), or prefer using `--params-json` (e.g., `--params-json '{"Foo":{"k":"v"}}'`). If you want to force-pass `true`/`123` etc. as strings, use JSON string format: `--param Name='"123"'`. For batch parameter passing, use `--params-json`; if the same key is passed, `--param` will override `--params-json`. Default is query string invocation, use `--json-body` for JSON body.

## Instance Management

Read-only operations:

```shell
python3 scripts/vephone_cli.py list-products --count 10
python3 scripts/vephone_cli.py list-pods <product_id> --max-results 10
python3 scripts/vephone_cli.py list-pods <product_id> --configuration-code-list <code1,code2> --dc-list <dc1,dc2> --online-list 1,2 --next-token <token>
python3 scripts/vephone_cli.py detail-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py get-pod-metric <product_id> <pod_id>
python3 scripts/vephone_cli.py get-pod-property <product_id> <pod_id>
```

State-changing operations:

```shell
python3 scripts/vephone_cli.py create-pod <product_id> --name <name> --template-id <template_id> --configuration-code <code> --dc-id <dc> --resource-type 200 --image-id <image_id> --display-layout-id <layout_id> --phone-template-id <template_id>
python3 scripts/vephone_cli.py update-pod <product_id> <pod_id> --image-id <image_id> --display-layout-id <layout_id> --configuration-code <code>
python3 scripts/vephone_cli.py update-pod <product_id> <pod_id> --image-id <image_id> --force
python3 scripts/vephone_cli.py delete-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py power-on-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py power-off-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py reboot-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py reset-pod <product_id> <pod_id>
python3 scripts/vephone_cli.py create-pod-one-step <product_id> --configuration-code <code> --dc <dc> --app-list <app_id:version_id,...>
python3 scripts/vephone_cli.py update-pod-property <product_id> --pod-id <pod_id> --pod-settings '[{"SettingsName":"locale_language","SettingsType":"global","SettingsValue":"zh-CN","SettingsValueType":"string"}]'
python3 scripts/vephone_cli.py update-pod-resource-apply-num <product_id> --resource-set-id <resource_set_id> --apply-num <num>
python3 scripts/vephone_cli.py backup-pod <product_id> --pod-id-list <pod_id1,pod_id2>
python3 scripts/vephone_cli.py restore-pod <product_id> --pod-id-list <pod_id1,pod_id2>
python3 scripts/vephone_cli.py pod-mute <product_id> --pod-id <pod_id> --mute true --display-list <display1,display2>
python3 scripts/vephone_cli.py pod-adb <product_id> --pod-id <pod_id> --enable true
python3 scripts/vephone_cli.py pod-stop <product_id> --pod-id <pod_id>
python3 scripts/vephone_cli.py pod-data-delete <product_id> --pod-id <pod_id> --file-path-list </sdcard,/data/data> --package-list <pkg1,pkg2>
python3 scripts/vephone_cli.py set-proxy <product_id> --pod-id-list <pod_id1,pod_id2> --proxy-status 1 --proxy-config '{"version":"v2","type":"socks5","address":"203.0.113.52","port":"12345"}'
python3 scripts/vephone_cli.py get-proxy <product_id> --pod-id-list <pod_id1,pod_id2>
python3 scripts/vephone_cli.py backup-data <product_id> --pod-id-list <pod_id1,pod_id2> --backup-all false --include-path-list </data/app,/data/data> --exclude-path-list </data/anr>
python3 scripts/vephone_cli.py restore-data <product_id> --backup-data-id <backup_data_id> --pod-id-list <pod_id1,pod_id2>
python3 scripts/vephone_cli.py list-backup-data <product_id> --status completed --max-results 20
python3 scripts/vephone_cli.py delete-backup-data <product_id> --backup-data-id-list <backup_data_id1,backup_data_id2>
```

`list-pods` supports pagination via `--max-results` and `--next-token`. `create-pod` can use `--image-id` to specify the initial image. Operations like `delete-pod`, `reset-pod`, `update-pod --force` have significant impact; confirm before execution. For structured parameters like `--app-list`, `--pod-settings`, `--specify-host-list`, `--proxy-config`, refer to the corresponding command's `-h` for format details.

## Resource, DC, Host, and Image Query

```shell
python3 scripts/vephone_cli.py list-dcs <product_id>
python3 scripts/vephone_cli.py get-dc-bandwidth-daily-peak <product_id> <dc_id_list>
python3 scripts/vephone_cli.py list-pod-resources <product_id>
python3 scripts/vephone_cli.py get-product-resource <product_id>
python3 scripts/vephone_cli.py list-products --count 10
python3 scripts/vephone_cli.py list-configurations <product_id>
python3 scripts/vephone_cli.py list-instance-configuration-specs <product_id>
python3 scripts/vephone_cli.py list-phone-templates <product_id>
python3 scripts/vephone_cli.py get-phone-template <product_id> <phone_template_id>
python3 scripts/vephone_cli.py list-hosts <product_id>
python3 scripts/vephone_cli.py detail-host <product_id> <host_id>
python3 scripts/vephone_cli.py update-host <product_id> <host_id1,host_id2> --configuration-code <pod_config_code>
python3 scripts/vephone_cli.py reboot-host <product_id> <host_id1,host_id2> --force
python3 scripts/vephone_cli.py reset-host <product_id> <host_id1,host_id2> --force
python3 scripts/vephone_cli.py list-image-resources <product_id>
python3 scripts/vephone_cli.py list-aosp-images <product_id> --is-public --max-results 20
python3 scripts/vephone_cli.py list-aosp-images <product_id> --max-results 20
python3 scripts/vephone_cli.py get-image-preheating <product_id> <image_id>
```

`list-image-resources` is used to view the image resources currently in use by the project. `list-aosp-images --is-public` queries public images; omitting `--is-public` queries custom images.

Before creating an instance, first check `list-pod-resources` and select a `(ConfigurationCode, Dc)` combination with available capacity, with the matching `--resource-type`.

Resource ordering operations are state-changing and may incur costs. Post-paid instance resource groups only apply to projects with existing order history; confirm before execution.

When ordering local storage hosts, explicitly pass the target pod specification, host type, data center, quantity, and resource type:

```shell
python3 scripts/vephone_cli.py subscribe-resource-auto \
  <product_id> \
  --configuration-code <pod_config_code> \
  --server-type-code <host_server_type_code> \
  --dc <dc> \
  --apply-num <num> \
  --resource-type 200 \
  --charge-type host_post_daily \
  --region <region> \
  --volc-region inner
```

Example for Wenzhou 03-ppe dual-open specification:

```shell
python3 scripts/vephone_cli.py subscribe-resource-auto \
  <product_id> \
  --configuration-code g3.pod8c24g.type2 \
  --server-type-code g3.host8c24g256g \
  --dc zjwz-ctcucm-03-47frx0k0 \
  --apply-num 3 \
  --resource-type 200 \
  --charge-type host_post_daily \
  --region cn-east \
  --volc-region inner
```

Before re-purchasing, confirm the post-paid resource group has order history via existing orders or resource records. After ordering or unsubscribing, use `list-pod-resources` and `list-hosts` to verify results.

Renewal and unsubscription:

```shell
python3 scripts/vephone_cli.py renew-resource-auto <product_id> --resource-set-id <resource_set_id> --term <n> --period <period> --round-id <round_id>
python3 scripts/vephone_cli.py unsubscribe-host-resource <product_id> <host_id1,host_id2> --force
```

After unsubscribing, use the resource list and host list to verify changes in host/resource counts.

Cloud disk storage uses `ResourceType=100`, local storage uses `ResourceType=200`.

## Application Management

Read-only operations:

```shell
python3 scripts/vephone_cli.py get-pod-app-list <product_id> <pod_id>
python3 scripts/vephone_cli.py list-apps <product_id>
python3 scripts/vephone_cli.py detail-app <product_id> <app_id>
python3 scripts/vephone_cli.py list-app-version-deploys <product_id> <app_id>
python3 scripts/vephone_cli.py get-app-crash-log <product_id> <pod_id1,pod_id2> --start-time <unix_seconds> --end-time <unix_seconds>
```

State-changing operations:

```shell
python3 scripts/vephone_cli.py install-app <product_id> <pod_id> <app_id> <version_id>
python3 scripts/vephone_cli.py launch-app <product_id> <pod_id> <package_name>
python3 scripts/vephone_cli.py close-app <product_id> <pod_id> <package_name>
python3 scripts/vephone_cli.py uninstall-app <product_id> <pod_id> <app_id>
python3 scripts/vephone_cli.py auto-install-app <product_id> --pod-id-list <pod_id1,pod_id2> --download-url <url>
```

`auto-install-app` can be installed via download URL or combined with image paths for batch installation. For specific parameter combinations, directly check `python3 scripts/vephone_cli.py auto-install-app -h`.


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
