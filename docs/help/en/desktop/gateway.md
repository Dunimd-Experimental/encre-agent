Gateway lets you interact with Encre Agent from the chat platforms you already use, and receive automation results there. Encre Agent supports **26+ chat platforms**.

## Supported platforms

In **Settings → Gateway** you see all adapters, for example:

QQ, Telegram, Discord, WeChat (iLink Bot / Web WeChat), WeCom, Feishu, DingTalk, Slack, WhatsApp, Signal, Matrix, Email (SMTP/IMAP), SMS, BlueBubbles, Webhook, Home Assistant, Google Chat, IRC, LINE, Mattermost, ntfy, Photon, Raft, SimpleX, Microsoft Teams, and more.

## Configuring a channel

1. Open **Settings → Gateway**
2. Find the adapter for your platform and click **Enable**
3. Fill in the credentials shown (fields vary by platform; common ones: Bot Token, App ID, App Secret, Webhook URL, phone number, etc.)
4. Click **Save**, then **Test connection**

Status is shown clearly: `Connected / Not connected / Connection error`.

![Gateway adapter list](/screenshots/gateway-list.png)

## WeChat example (QR login)

The WeChat adapter supports **QR code login**:

1. Choose the QR login option in the WeChat adapter
2. Click **Connect**; a **QR code** is shown (with a countdown)
3. Scan it with WeChat and authorize
4. Once connected, chat with the agent through that WeChat account
5. **Unlink** anytime if needed

> [!NOTE]
> The QR code has an expiry time. If it shows "QR code expired", hit refresh and scan again.

## Common settings

Some channels offer more options:

| Setting | Description |
| --- | --- |
| Target model | The model used for that channel's chats; leave empty for no limit |

## What gateway is for

- **Chat**: talk with the agent inside chat platforms
- **Receive automation results**: scheduled tasks push outcomes to the gateway
- **Notifications**: alerts when tasks finish or hit problems

See [Automation](/en/desktop/automation) for the full push workflow.

> [!TIP]
> If a test connection fails, check that the Token/Secret was copied completely, the account has the required permissions, and outgoing network access works.