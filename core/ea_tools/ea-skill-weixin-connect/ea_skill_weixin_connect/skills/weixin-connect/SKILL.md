---
name: weixin-connect
description: "Connect personal WeChat (not Enterprise WeChat). Used when user says \"connect personal WeChat\", \"integrate personal WeChat\", \"bind personal WeChat\", \"scan personal WeChat QR code\". Note: If user says \"Enterprise WeChat\" or \"WeCom\", this skill is not applicable, use wecom-connect skill. Once this skill is matched, must strictly follow the process to completion."
hidden: true
context: inline
---

## Weixin Connect
# Personal WeChat Connect Skill

## ⚠️ Matching Rules (Strictly Differentiate, Do Not Confuse)

**This skill only applies to personal WeChat, triggered by the following keywords:**
- "Connect personal WeChat" / "Integrate personal WeChat" / "Bind personal WeChat"
- "Personal WeChat QR scan" / "WeChat QR code login"
- "Connect WeChat" (when not accompanied by "Enterprise", defaults to personal WeChat)

**The following keywords do NOT belong to this skill, triggering is prohibited:**
- "Enterprise WeChat" / "WeCom" / "wecom" / "WeCom" → Use wecom-connect skill

**Once this skill is read, must strictly follow the process below from Step 0 to completion, no skipping steps, no improvisation, no reading other documents.**

## Core Principles

- **Strictly follow the steps, don't add anything extra.**
- **QR code display prioritizes CDN, always keep workspace backup.**
- **Do not auto-poll.** After giving the QR code, wait for the user to say "scanned" then poll.
- **Do not manually modify `Encre.json`.**

## Execution Flow (Hardcoded, Follow Exactly)

### Step 0: Check Plugin, Install if Missing

```shell
ls ~/.Encre/extensions/Encre-weixin/package.json 2>/dev/null && echo "INSTALLED" || echo "NOT_INSTALLED"
```

- `INSTALLED` → Skip to Step 1
- `NOT_INSTALLED` → Install:

```shell
npx -y @tencent-weixin/Encre-weixin-cli install 2>&1
```

After installation, verify `ls ~/.Encre/extensions/Encre-weixin/package.json`, confirm `INSTALLED` then continue.

### Step 1: Call ilink API to Get QR Code

```shell
curl -s "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
```

Extract from returned JSON:
- `qrcode` — Save, used for polling
- `qrcode_img_content` — QR code URL, used to generate PNG

### Step 2: Generate PNG → upload_to_cdn + workspace Dual Backup

**Do it all in one go.**

```shell
cd /tmp && npm install qrcode 2>/dev/null | tail -1
```

```shell
cd /tmp && node -e "const qr=require('qrcode'); qr.toFile('/tmp/weixin_qr.png','<qrcode_img_content>',{width:400,margin:2},(e)=>{if(e)console.error(e);else console.log('saved');})"
```

**Do both things simultaneously:**

1. upload_to_cdn:

```
upload_to_cdn /tmp/weixin_qr.png
```

2. Save workspace backup (regardless of CDN success):

```shell
cp /tmp/weixin_qr.png ~/workspace/weixin_qr.png
```

CDN result handling:
- **Success** → Step 3 uses CDN URL for display
- **Failure** → Retry upload_to_cdn, max 3 times
- **3 times still fail** → Use workspace backup as fallback

No alternative upload methods allowed (0x0.st, catbox, imgbb, base64, canvas, Encre upload, etc. all prohibited).

### Step 3: Display QR Code, Wait for User to Scan

**When CDN succeeds (recommended guidance):**

---

## WeChat QR Code Login

Use **WeChat** to scan the QR code below:

<CDN image URL>

(Backup image saved to ~/workspace/weixin_qr.png)

**Steps:**
1. Open **WeChat** App on your phone
2. Scan the QR code above
3. Confirm login on your phone
4. After scanning, tell me "ok", I'll continue with the next steps

⏱ Valid for approximately 1 minute

---

**When CDN fails (workspace fallback):**

---

## WeChat QR Code Login

QR code saved to `~/workspace/weixin_qr.png`, please open the file and scan with **WeChat**.

After scanning, confirm login on your phone, then tell me "ok".

⏱ Valid for approximately 1 minute

---

Then **stop, wait for user confirmation**.

### Step 4: After User Confirms → Poll + Write Credentials + Restart

**4a. Poll Status:**

```shell
curl -s "https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=<qrcode>"
```

| status | Action |
|---|---|
| `wait` | Wait 3 seconds then poll again |
| `scaned` | Tell user "QR code scanned, please confirm login on your phone" |
| `confirmed` | Success! Extract `ilink_bot_id`, `bot_token`, `baseurl`, `ilink_user_id` |
| `expired` | Restart from Step 1 |

**4b. Write Credentials (must execute after confirmed):**

Replace `@` → `-` and `.` → `-` in `ilink_bot_id` to get `accountId` (example: `a34b410e2e6f@im.bot` → `a34b410e2e6f-im-bot`).

Write a temporary script to execute:

```shell
cat > /tmp/write_weixin_account.js << 'SCRIPT'
const fs = require('fs');
const path = require('path');
const home = process.env.HOME;

const accountId = '__ACCOUNT_ID__';
const data = {
  token: '__ILINK_BOT_ID__:__BOT_TOKEN__',
  savedAt: new Date().toISOString(),
  baseUrl: '__BASEURL__',
  userId: '__ILINK_USER_ID__'
};

const accountsDir = path.join(home, '.Encre/Encre-weixin/accounts');
fs.mkdirSync(accountsDir, { recursive: true });

const accountFile = path.join(accountsDir, accountId + '.json');
fs.writeFileSync(accountFile, JSON.stringify(data, null, 2));
fs.chmodSync(accountFile, 0o600);

const indexPath = path.join(home, '.Encre/Encre-weixin/accounts.json');
let existing = [];
try { existing = JSON.parse(fs.readFileSync(indexPath, 'utf-8')); } catch {}
if (!existing.includes(accountId)) existing.push(accountId);
fs.writeFileSync(indexPath, JSON.stringify(existing, null, 2));

console.log('Credentials + index written successfully');
SCRIPT
```

Replace placeholders in the script with actual values then execute:

```shell
sed -i 's/__ACCOUNT_ID__/<accountId>/g; s/__ILINK_BOT_ID__/<ilink_bot_id>/g; s/__BOT_TOKEN__/<bot_token>/g; s/__BASEURL__/<baseurl>/g; s/__ILINK_USER_ID__/<ilink_user_id>/g' /tmp/write_weixin_account.js && node /tmp/write_weixin_account.js
```

**4c. Restart Gateway:**

```shell
Encre gateway restart
```

### Success Response

---

## WeChat Connection Result

- ✅ Status: Successfully bound
- ✅ Gateway: Restarted
- ilink_bot_id: `<ilink_bot_id>`
- ilink_user_id: `<ilink_user_id>`

Now you can send messages directly in WeChat 🎉

---

That's it. Do not perform any additional operations. Do not read documents.

## Absolutely Prohibited

- Skip upload_to_cdn (only use workspace fallback after 3 failures)
- Auto-start polling (wait for user to say "scanned / ok")
- Manually modify `Encre.json`
- Skip credential writing step (without credentials the plugin cannot connect)
- Give user the raw `qrcode_img_content` URL
- Use alternative upload methods (0x0.st, catbox, imgbb, base64, canvas, etc.)

## One-sentence Summary

Check plugin → curl to get QR code → PNG → CDN + workspace dual backup → User scans with WeChat → Poll for confirmation → Write credential file → Restart gateway → Done.
