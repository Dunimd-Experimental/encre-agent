---
name: byted-sms-sender
description: Volcengine SMS service management tool. Use this capability when you need cloud communication features, including sending SMS, querying message groups, template information, sending details, status, and overall sending statistics.
---

# Byted SMS Sender

Volcengine SMS Service API, Version 2026-01-01

## When to Use

Use this skill when the user has the following needs:

**SMS Sending Scenarios:**

- Need to send verification code SMS
- Need to send notification SMS
- Need to send marketing SMS
- User says "send SMS", "send verification code", "send notification"

**Query Scenarios:**

- Need to query available message groups (sub-accounts)
- Need to query approved SMS signatures
- Need to query approved SMS templates
- Need to query SMS sending records
- Need to query sending statistics (success rate, etc.)

## Pre-Use Checks

Check whether the following credentials are configured:

- `ARK_SKILL_API_KEY` - API Key
- `ARK_SKILL_API_BASE` - API Base URL

These credentials are pre-configured in the terminal environment by **ArkClaw**. Configuration file location: `/root/.openclaw/.env`

Check method:
```shell
echo $ARK_SKILL_API_KEY
echo $ARK_SKILL_API_BASE
```

If credentials are missing:
1. Check whether the configuration file `/root/.openclaw/.env` exists
2. If still not found, contact **oncall** for assistance

## 6 API Descriptions

### 1. send\_sms - Send SMS

**Scenario:** User needs to send verification code, notification, or marketing SMS

**Usage:**

```shell
python3 scripts/volc_sms.py send_sms \
  --sub-account "Message Group ID" \
  --signature "Signature" \
  --template-id "Template ID" \
  --mobiles "Phone Number" \
  --template-param '{"code":"123456"}'
```

**Parameter Description:**

- `--sub-account`: Message Group ID (required), obtained from list\_sub\_account
- `--signature`: SMS signature (required), obtained from list\_signature
- `--template-id`: Template ID (required), obtained from list\_sms\_template
- `--mobiles`: Phone number(s) (required), multiple separated by commas
- `--template-param`: Template parameters (optional), JSON format

### 2. list\_sub\_account - Query Message Groups

**Scenario:** Need to know which message group can be used to send SMS

**Usage:**

```shell
python3 scripts/volc_sms.py list_sub_account
```

**Parameter Description:**

- `--sub-account-name`: Optional, fuzzy search by name

### 3. list\_signature - Query Signatures

**Scenario:** Need to know which signature can be used, or check whether a signature has been approved

**Usage:**

```shell
python3 scripts/volc_sms.py list_signature --signature "Volcengine"
```

**Parameter Description:**

- `--signature`: Optional, fuzzy search by signature
- `--sub-accounts`: Optional, filter by sub-account
- `--page`: Page number, default 1
- `--page-size`: Items per page, default 20

### 4. list\_sms\_template - Query Templates

**Scenario:** Need to know which template can be used, or query template parameters

**Usage:**

```shell
python3 scripts/volc_sms.py list_sms_template --signatures "Volcengine"
```

**Parameter Description:**

- `--template-id`: Optional, fuzzy search by template ID
- `--signatures`: Optional, filter by signature
- `--sub-accounts`: Optional, filter by sub-account
- `--page`: Page number, default 1
- `--page-size`: Items per page, default 20

### 5. list\_sms\_send\_log - Query Send Logs

**Scenario:** Need to view the sending status of a specific SMS, or batch query sending history

**Usage:**

```shell
python3 scripts/volc_sms.py list_sms_send_log \
  --sub-account "Message Group ID" \
  --from-time 1773113285 \
  --to-time 1773213285
```

**Parameter Description:**

- `--sub-account`: Required, Message Group ID
- `--from-time`: Start timestamp (seconds)
- `--to-time`: End timestamp (seconds)
- `--mobile`: Optional, filter by phone number
- `--template-id`: Optional, filter by template ID
- `--signature`: Optional, filter by signature
- `--message-id`: Optional, exact query by message ID
- `--page`: Page number, default 1
- `--page-size`: Items per page, default 100

### 6. list\_total\_send\_count\_stat - Query Send Statistics

**Scenario:** Need to view statistics such as send success rate, delivery success rate, etc.

**Usage:**

```shell
python3 scripts/volc_sms.py list_total_send_count_stat \
  --start-time 1773113285 \
  --end-time 1773213285
```

**Parameter Description:**

- `--start-time`: Required, start timestamp (seconds)
- `--end-time`: Required, end timestamp (seconds)
- `--sub-account`: Optional, filter by message group
- `--channel-type`: Optional, channel type
- `--signature`: Optional, filter by signature
- `--template-id`: Optional, filter by template ID

**Return Fields:**

- TotalSendCount: Total sent count
- TotalSendSuccessCount: Send success count
- TotalSendSuccessRate: Send success rate
- TotalReceiptSuccessCount: Delivery success count
- TotalReceiptSuccessRate: Delivery success rate

## Typical Usage Flow

### First Time Sending SMS

1. **Query available message groups**
   ```shell
   python3 scripts/volc_sms.py list_sub_account
   ```
2. **Query available signatures**
   ```shell
   python3 scripts/volc_sms.py list_signature
   ```
3. **Query available templates**
   ```shell
   python3 scripts/volc_sms.py list_sms_template --signatures "Volcengine"
   ```
4. **Send SMS**
   ```shell
   python3 scripts/volc_sms.py send_sms \
     --sub-account "xxxx" \
     --signature "xxx" \
     --template-id "ST_xxxx" \
     --mobiles "188xxxxxxx8" \
     --template-param '{"code":"888888"}'
   ```

### Query Sending Status

```shell
python3 scripts/volc_sms.py list_sms_send_log \
  --sub-account "77da1acf" \
  --from-time 1773113285 \
  --to-time 1773213285
```

## Common Error Codes

- `RE:0001`: Account SMS service not activated
- `RE:0003`: Sub-account does not exist (incorrect message group ID)
- `RE:0004`: Signature error (signature does not exist or not approved)
- `RE:0005`: Template error (template does not exist or not approved)
- `RE:0006`: Phone number format error
- `RE:0010`: Account overdue
- `ZJ10200`: Request parameter error

## Notes

1. **Signatures and Templates**: Must use approved signatures and templates
2. **Phone Number Format**:
   - Domestic SMS: 11-digit phone number or prefixed with +86
   - International SMS: Must include international dialing code, compliant with E.164 standard
3. **Batch Limit**: Maximum 200 phone numbers per request
4. **Signature-SubAccount Matching**: Signatures and message groups need to match; can be verified from the SubAccounts field of list\_signature
5. **Template-Signature Matching**: Templates and signatures need to match; can be verified from the Signature field of list\_sms\_template

## Troubleshooting

- Missing credentials: Check `/root/.openclaw/.env` file; if still not found, contact oncall
- Send failure: First use list\_sub\_account, list\_signature, list\_sms\_template to confirm parameters are correct
- Authentication failure: Check whether your configured AK/SK has been activated correctly
- Permission error: Check whether the credentials are correct; if the issue persists, contact oncall
- Overdue error: Contact oncall for resolution
