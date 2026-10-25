---
name: wechat-auto-reply
description: "Semi-automatic reply to WeChat contact messages (auto-send when confidence >85%, otherwise confirm), or proactively send content."
aliases: [wechat-reply, wechat-auto]
argument_hint: "Contact name and optional message content"
user_invocable: true
hidden: true
---
context: inline
---

## Wechat Auto Reply
# WeChat Auto Reply Skill

Semi-automatic reply to WeChat contact messages (intelligent judgment based on AI confidence), or proactively send specified content.

## 🚀 Installation

### Using Homebrew (Recommended)

```shell
# One-line install
`terminal brew install` bjdzliu/Encre/wechat-auto-reply

# Or install in two steps
brew tap bjdzliu/Encre
`terminal brew install` wechat-auto-reply
```

After installation, it will automatically:
- Install all dependencies (`cliclick`, `python@3`, `pyobjc`)
- Create global command `wechat-auto-reply`
- Set Encre skill link to `~/.Encre/workspace/skills/wechat-auto-reply`

## 💡 Usage

```shell
# OCR semi-auto reply (reads chat history, decides reply intelligently)
# Auto-send when confidence > 85%, otherwise show a confirmation dialog
wechat-auto-reply "联系人名称" (contact name)

# Proactive send (sends the given message directly, no OCR)
wechat-auto-reply "联系人名称" (contact name) "消息内容" (message content)
```

**Examples:**
```shell
# Semi-auto reply mode
wechat-auto-reply "小李"      # Auto-sends in high-confidence scenarios like "在吗"
wechat-auto-reply "小王"      # For question-type messages, a popup asks you to confirm or modify

# Proactive send mode
wechat-auto-reply "小李" "什么时候下班"
wechat-auto-reply "小王" "今天行情怎么样"
```

## Features

**Two Modes:**
1. **Semi-automatic Reply Mode**: Search contact → OCR recognize chat content → AI judge reply
   - Confidence > 85% → Auto send
   - Confidence ≤ 85% → Popup confirmation (can modify reply content)
2. **Proactive Send Mode**: Search contact → Directly send specified message

## 📂 File Locations

### After Homebrew Installation
- **Skill Directory**: `$(brew --prefix)/share/Encre/skills/wechat-auto-reply`
- **User Link**: `~/.Encre/workspace/skills/wechat-auto-reply`
- **Global Command**: `$(brew --prefix)/bin/wechat-auto-reply`
- **Config File**: `~/.Encre/workspace/skills/wechat-auto-reply/wechat-dm.applescript`

### View Installation Path
```shell
which wechat-auto-reply
ls -la ~/.Encre/workspace/skills/wechat-auto-reply
```

## Environment Setup

### Via Homebrew (Recommended)

All dependencies are installed automatically, no manual configuration needed.

### Manual Dependency Installation

#### Dependency Tools

| Tool | Installation | Purpose |
|------|-------------|---------|
| `cliclick` | ``terminal brew install` cliclick` | Stable mouse clicks |
| `screencapture` | macOS Built-in | Screenshot (can be called via `/usr/sbin/screencapture`) |
| Vision Framework | macOS 10.15+ | OCR text recognition |

#### Python Dependencies

```shell
pip3 install pyobjc
```

## Implementation

### 1. Activate WeChat

```applescript
tell application "WeChat" to activate
```

### 2. Ensure Foreground

```applescript
tell app "System Events"
  tell process "WeChat"
    set frontmost to true
  end tell
end tell
```

### 3. Search Contact

- Use `Cmd+F` to open search
- Paste contact name via clipboard
- Press Enter to enter chat

### 4. OCR Screenshot

Use macOS Vision Framework to recognize chat content:

```python
from Vision import VNRecognizeTextRequest, VNImageRequestHandler

theRequest.setRecognitionLanguages(["zh-Hans", "en-US"])
theRequest.setUsesLanguageCorrection(True)
```

### 5. Intelligent Reply Judgment (with Confidence)

Automatically generate replies based on chat content, each reply comes with a confidence score:

| Scenario | Keywords | Reply Content | Confidence |
|----------|----------|---------------|------------|
| Asking if online | "" (Are you there), "" (Busy?) | "" (Yes, what's up?) | 95% |
| Thanking reply | "" (Thanks), "" (Thank you) | "" (You're welcome) | 95% |
| Confirming info | ""+"" (Got it + Okay) | "" (Okay) | 90% |
| Investment discussion | "" (Invest), "" (Buy the dip), "" (Market) | "" (No rush, wait until stable) | 85% |
| Question consultation | "?""" | "" (Let me check, hold on) | 75% |
| General confirmation | "" (Ok), "OK" | "" (Okay) | 80% |
| Time related | "" (Tomorrow), "" (What time) | "" (Let me confirm and get back to you) | 70% |
| Default reply | Other | "" (Got it) | 60% |

**Confidence Rules:**
- **≥ 85%**: Direct auto-send (high confidence scenario)
- **< 85%**: Popup shows suggested reply, needs user confirmation
  - Can choose "Confirm Send" to send directly
  - Can choose "Modify Reply" to manually edit content
  - Can choose "Cancel" to not send

### 6. Send Message

- Click input box to get focus
- Paste reply content
- Press Enter to send

## Important Notes

- **Input Box Coordinates**: Default `{1000, 832}`, adjust based on actual screen
- **OCR Recognition**: Supports Chinese and English, set `["zh-Hans", "en-US"]`
- **Wait Time**: Recommended to wait 0.5-1s after each operation
- **Clipboard**: Using AppleScript `set the clipboard` is more reliable than `pbcopy`
- **Confidence Threshold**: Default 85%, can adjust the `if confidence > 85` line in the script
- **Confirmation Popup**: At low confidence, shows full chat content and suggested reply, supports manual editing

## Custom Configuration

### Modify Input Box Coordinates

Find the config file location:
```shell
# Homebrew install
vim ~/.Encre/workspace/skills/wechat-auto-reply/wechat-dm.applescript

# Or use the brew path
vim $(brew --prefix)/share/Encre/skills/wechat-auto-reply/wechat-dm.applescript
```

Modify coordinates:
```applescript
cliclick c:1000,832  # Change to your coordinates
```

### Adjust Confidence Threshold

Edit config file:
```applescript
if confidence > 85 then  # Change to your desired threshold (0-100)
  set autoSend to true
```

### Add Custom Reply Rules

Add in the intelligent reply judgment section:
```applescript
else if ocrResult contains "你的Keyword" (your keyword) then
  set replyText to "你的回复内容" (your reply content)
  set confidence to 90  -- Set confidence
```

## Update & Uninstall

### Update
```shell
brew upgrade wechat-auto-reply
```

### Uninstall
```shell
brew uninstall wechat-auto-reply

# Optional: remove the tap
brew untap bjdzliu/Encre
```

## Error Handling

- WeChat not installed: prompt to install WeChat
- Search no results: prompt that contact does not exist
- OCR failure: retry screenshot or use alternative method