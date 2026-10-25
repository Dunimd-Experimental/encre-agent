---
name: alipay-payment-integration
description: Best practice guide for integrating Alipay payment products, covering online and offline payment scenarios.
hidden: true
context: inline
---

## Alipay Payment Integration

All Alipay payment product documentation is available via online dynamic links. Before integrating, always read the corresponding product's online documentation to get the latest API parameters and code examples.

### Documentation Access

Access Alipay online documentation using curl:

```shell
# Example: Get face-to-face payment docs
curl -sL "https://ideservice.alipay.com/cms/site/0izcu3"
```

#### Recursive Access
Document pages contain links that need recursive access for complete content:

1. First access the main document URL
2. Parse links in the document (product introduction, integration preparation, API docs, etc.)
3. Recursively access these links for detailed content

```shell
# Example: Access face-to-face payment sub-links
curl -sL "https://ideservice.alipay.com/cms/site/0izal0"   # Product introduction
curl -sL "https://ideservice.alipay.com/cms/site/0izal1"   # Integration preparation
```

### Integration Process

#### Step 1. Collect Integration Information
Before integrating, read the following based on user input:

- **SDK Selection**: Choose the General or Easy SDK based on your development language. Download link
- **Signing Method**: Supports RSA and RSA2; RSA2 (SHA256WithRSA) is recommended. Signing guide

#### Step 2. Get Product Integration Documentation
Get complete integration info based on the routed product: read the quick start guide, complete API documentation, async notification guide, notes, etc. Collect as much info as possible based on user input using curl.

Must read: Integration specifications and common pitfalls. Integration guide

Common error codes reference. Error code docs

#### Step 3. Integration Verification
Verify during integration and before going live: ensure signing/verification, async notifications, and exception handling comply with specifications. Verification results are for reference; always check the latest Alipay open platform documentation. See: Integration verification checklist

### Routing Table
Route to the corresponding product documentation based on the user's business scenario:

| Scenario | Recommended Product | Core API | Online Docs |
|----------|-------------------|----------|-------------|
| Offline store: user shows payment code, merchant scans with scanner | Face-to-Face Payment | alipay.trade.pay | [Face-to-Face Docs](https://ideservice.alipay.com/cms/site/0izcu3) |
| Merchant generates QR code, user scans to pay | QR Code Payment | alipay.trade.precreate | [QR Code Docs](https://ideservice.alipay.com/cms/site/0izcwi) |
| Mobile browser H5 page invokes Alipay payment | Mobile Web Payment | alipay.trade.wap.pay | [Mobile Web Docs](https://ideservice.alipay.com/cms/site/0izcwm) |
| Desktop browser web page redirects to Alipay checkout | Desktop Web Payment | alipay.trade.page.pay | [Desktop Web Docs](https://ideservice.alipay.com/cms/site/0izcws) |
| Alipay mini-program payment | JSAPI Payment | alipay.trade.create + my.tradePay | [JSAPI Docs](https://ideservice.alipay.com/cms/site/0izcx8) |
| Native iOS/Android/HarmonyOS App payment | App Payment | alipay.trade.app.pay | [App Payment Docs](https://ideservice.alipay.com/cms/site/0izcxe) |
| Deposit freeze, credit stay, deposit-free rental | Pre-authorization | alipay.fund.auth.order.app.freeze | [Pre-auth Docs](https://ideservice.alipay.com/cms/site/0izcxk) |
| Recurring billing, auto-renewal, membership subscription | Merchant Deduction | alipay.trade.app.pay (pay & sign) + alipay.trade.pay (subsequent) | [Merchant Deduction Docs](https://ideservice.alipay.com/cms/site/0izcxs) |

Before answering any integration questions or writing code, first read the corresponding online documentation links in the table above using curl. The documentation contains the latest API parameters, code examples, and notes.

### Decision Tree
```text
User inquires about Alipay integration
        |
        +-- Offline store payment?
        |       +-- User shows code, merchant scans --> Face-to-Face Payment
        |       +-- Merchant shows QR, user scans --> QR Code Payment
        |
        +-- Online payment?
        |       +-- Native App (iOS/Android/HarmonyOS) --> App Payment
        |       +-- Alipay mini-program --> JSAPI Payment
        |       +-- Mobile browser H5 --> Mobile Web Payment
        |       +-- Desktop browser --> Desktop Web Payment
        |
        +-- Need deposit/freeze?
        |       +-- Pre-authorization
        |
        +-- Recurring auto-debit?
                +-- Subscription/auto-renewal --> Merchant Deduction
```

### Keyword Matching
| Keywords | Routed Product |
|----------|----------------|
| payment code, barcode payment, scanner, passive scan, offline store, convenience store, supermarket, restaurant, POS scan, physical store, face-to-face, user shows payment code | Face-to-Face Payment |
| order code, merchant QR, active scan, pre-create, merchant generates QR, user scans to pay, product QR, pre-create order | QR Code Payment |
| H5 payment, WAP payment, mobile web, mobile browser, mobile webpage, wap checkout, mobile H5 | Mobile Web Payment |
| PC payment, computer web, web payment, PC browser, website payment, computer checkout | Desktop Web Payment |
| mini-program payment, JSAPI, Alipay mini-program, life account, in-app payment, mini-program checkout, my.tradePay | JSAPI Payment |
| App payment, mobile app payment, iOS payment, Android payment, HarmonyOS payment, in-app payment, native App, SDK payment | App Payment |
| pre-authorization, deposit, fund freeze, credit stay, deposit-free, pay-after-use, hotel deposit, car rental deposit, power bank deposit, bike deposit | Pre-authorization |
| recurring billing, auto-renewal, membership subscription, continuous monthly, merchant deduction, periodic deduction, subscription, merchant deduction | Merchant Deduction |

### Clarification Script
When the user's description is unclear:

Please confirm your business scenario:

1. Offline Store Payment
   - Face-to-Face Payment: User shows payment code, merchant scans with scanner
     Suitable for: convenience stores, supermarkets, restaurants, hospitals, schools, cinemas, tourist attractions
   - QR Code Payment: Merchant generates QR code, user scans to pay
     Suitable for: product sales, media advertising payment

2. In-App Payment
   - Native iOS/Android/HarmonyOS App invokes Alipay payment
   - Falls back to H5 payment when Alipay app is not installed

3. Alipay Mini-Program Payment
   - JSAPI Payment: invoke Alipay checkout within mini-program
   - Suitable for: in-mini-program shopping, service purchases

4. Mobile Web Payment
   - Mobile browser H5 page invokes Alipay App or web checkout
   - Suitable for: mobile webpage payment

5. Desktop Web Payment
   - Desktop browser redirects to Alipay web checkout
   - Supports QR code scan or account login payment
   - Suitable for: PC e-commerce, online service platforms

6. Pre-authorization Payment
   - Freeze funds or credit limit first, deduct actual consumption, release remaining
   - Suitable for: hotels, car rental, bike rental, power bank rental, electronics rental

7. Merchant Deduction (Recurring Auto-Debit)
   - After user signs authorization, merchant initiates periodic deductions
   - Suitable for: membership subscriptions, auto-renewal, periodic repayment

Please describe your specific business needs?

### Security Red Lines
The following are security red lines for Alipay payment integration. Violations may result in financial loss or security incidents. Must be strictly followed. Remind users to verify against the integration checklist and Alipay open platform documentation before going live.

- **Private key must NOT be stored on client**: Transaction data construction and signing must be done on the merchant server; private keys must NEVER be stored in the App client.
- **Private key must NOT be logged**: Private keys must NOT appear in any logs.
- **Private key must NOT be uploaded to public repositories**: Private keys must NOT be uploaded to GitHub, GitLab, or other public code repositories.
- **Front-end payment result is NOT trustworthy**: Synchronous redirect results from the front-end are not trustworthy. Always use Alipay async notifications or query API to confirm payment results.
- **Do NOT re-charge before confirmation**: Do not ask the user to pay again before confirming the payment result. Always confirm via async notification or query API first.
- **Async notification must be verified**: Always verify the signature of async notifications to ensure they come from Alipay.

### Environment
- **Sandbox**: `https://openapi-sandbox.dl.alipaydev.com/gateway.do`. [Sandbox Guide](https://open.alipay.com/platform/sandbox.htm)
- **Production**: `https://openapi.alipay.com/gateway.do`

### Notes
- This skill does NOT support merchant split accounting. Please refer to the open platform documentation or contact Alipay technical support.
- For business inquiries and integration questions, please refer to the [Open Platform Documentation](https://open.alipay.com) or contact Alipay technical support.
- Merchant Deduction product completed an upgrade on April 2, 2026. This document only supports the latest version. Existing merchants using the old version can continue using it, but no further capability updates or new scenario access will be provided.
- Merchant Deduction currently only supports recurring deductions; user-initiated password-free payment scenarios are not supported yet.
- Developers are recommended to use the sandbox environment during testing.
- All links in this document point to Alipay online documentation, which is dynamically updated. Always read the latest version before writing code.

### Related Skills
- `alipay` - General Alipay integration framework
- `payment-alipay` - Alipay end-user operations guide
