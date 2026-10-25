网关让你通过常用聊天平台与 Encre Agent 互动，也可以接收自动化任务的结果。Encre Agent 支持 **26+ 聊天平台**的接入。

## 支持的平台

在 **设置 → 网关** 中可以看到全部适配器，例如：

QQ、Telegram、Discord、微信（iLink Bot / Web 微信）、企业微信（WeCom）、飞书、钉钉、Slack、WhatsApp、Signal、Matrix、Email（SMTP/IMAP）、短信、BlueBubbles、Webhook、Home Assistant、Google Chat、IRC、LINE、Mattermost、ntfy、Photon、Raft、SimpleX、Microsoft Teams 等。

## 配置一个渠道

1. 打开 **设置 → 网关**
2. 找到目标平台的适配器，点击**启用**
3. 按页面字段填写凭证（不同平台的字段不同，常见的有：Bot Token、App ID、App Secret、Webhook URL、手机号等）
4. 点击**保存**，然后**测试连接**

连接状态一目了然：`已连接 / 未连接 / 连接错误`。

![网关适配器列表](/screenshots/gateway-list.png)

## 微信接入示例（扫码）

微信渠道支持 **扫码连接**：

1. 在微信适配器中选择扫码登录方式
2. 点击「连接」，页面会生成**二维码**（带剩余时间）
3. 用微信扫码授权
4. 连接成功后即可通过该微信与 Agent 对话
5. 需要时可随时**解绑**

> [!NOTE]
> 二维码有时效性。若显示「二维码已失效」，点击刷新后重新扫码即可。

## 常见设置项

部分渠道提供更多选项：

| 设置项 | 说明 |
| --- | --- |
| 目标模型 | 该渠道对话使用的模型，留空表示无限制 |

## 网关的用途

- **对话**：在聊天平台里和 Agent 对话
- **接收自动化结果**：定时任务执行完把结果推送到网关
- **通知**：任务完成、遇到问题时的提醒

配置自动化推送的完整流程见 [自动化任务](/desktop/automation.md)。

> [!TIP]
> 测试连接失败时，先核对 Token / Secret 是否复制完整、账号是否授权了所需权限；也检查应用出网是否正常。