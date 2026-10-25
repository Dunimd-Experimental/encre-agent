# DouyinPay Coding Standards and Guidelines

This document serves as the coding standard reference. Follow these guidelines when generating code for users.

---

## I. Code Structure Standards

Standard code structure follows this order:
Import dependencies → Config constants (placeholders) → Core logic → Error handling

### 1.1 Placeholder Conventions

All sensitive configuration uses unified placeholders with corresponding comments for easy identification and replacement:

| Placeholder | Comment & Description |
|-------------|----------------------|
| `YOUR_MCH_ID` | Merchant ID, obtained after registering on DouyinPay merchant platform |
| `YOUR_APP_ID` | App AppID (must be bound in Merchant Platform - Product Center - AppID Management) |
| `YOUR_PRIVATE_KEY_PATH` | Private key file path corresponding to the merchant public key certificate (RSA) |
| `YOUR_MERCHANT_CERT_SERIAL_NO` | Merchant public key certificate serial number (`serial_no` in HTTP Authorization header) |
| `YOUR_PUBLIC_KEY_PATH` | DouyinPay public key certificate file path from the platform (for callback verification) |
| `YOUR_API_ENCRYPT_KEY` | API encryption key (symmetric key for AES-256-GCM decryption of sensitive fields in callback messages) |
| `YOUR_NOTIFY_URL` | Payment result callback notification URL (must be HTTPS, publicly accessible, no parameters; see docs) |
| `YOUR_RETURN_URL` | Frontend redirect return URL (H5 payment scenario) |

Private keys and CSR can be generated using `terminal scripts/gen_rsa_key.sh`.

#### Client Code Does Not Use Placeholders

Client (iOS / Android / HarmonyOS) payment invocation code should follow the official document example style: use dictionary literals with example values and Chinese comments to explain field meanings. Do NOT use `YOUR_XXX` placeholders. Reason:

- Payment invocation parameters are returned completely by the server after pre-order creation; the client merely passes them through
- Official docs use example values, which users find easier to understand
- See `references/app-sdk-integration-guide.md` Section 14 for details

### 1.2 Comment Standards

- Use Chinese comments for key steps
- Prefer referencing original code and parameter descriptions from official docs; do not fabricate
- Include corresponding official doc links in comments: `// Reference: https://pay.douyinpay.com/wiki/xxx/yyy`
- Function/method-level comments should describe parameters, return values, and possible exceptions

### 1.3 Security Standards

- **Private key**: Read from file or environment variable; NEVER hardcode in source code
- **Callback verification**: Always verify signature before processing business logic upon receiving payment callback
- **Do not trust frontend**: Do not rely on frontend payment results; use server-side callbacks as the source of truth
- **Idempotency**: Callback endpoints must support idempotency to avoid duplicate processing; order creation must also prevent duplicate orders
- **Parameter expiry**: Be aware of validity periods and expiry times for parameters; check if re-acquisition is needed
- **Element consistency check**: After callback/order query verification, check that key fields match the local order (merchant ID/app ID/order number/amount/currency, etc.); alert on mismatch

### 1.4 README.md Standards

Before finishing code generation, **must** create or update a README.md file:

1. If a `README.md` already exists in the project, create a new `DOUYINPAY_README.md` instead
2. Content must include:
   - Generated code file structure description
   - Key dependencies introduced
   - Key configuration instructions
   - Project initialization and frontend/backend startup commands

### 1.5 Modular Structure Requirements

- Use modular, atomic design; do not pile all logic into a single file

---

## II. SDK Support

### 2.1 Server SDK

Official SDKs are available for Go, Java, and PHP. When generating project dependency configuration (e.g. `go.mod`, `pom.xml`, `composer.json`), **must obtain the real latest version number** (e.g. Maven coordinate version, Go repo tag, etc.).

**Commands to obtain real version numbers:**

1. First call `search_docs` tool to check official integration docs for dependency import examples and extract version numbers
2. If the docs do not clearly specify the latest version, **must** go to the corresponding code repository to get the latest Release version:
   - Go: Search `github.com/douyinpay/douyinpay-go` for the latest tag
   - Java: Search Maven Central `central.sonatype.com/artifact/io.github.douyinpay/douyinpay-java` for the latest coordinate version

Note: **Never fabricate or hallucinate version numbers** (do NOT use `v0.0.0-latest`, `1.0.0`, or other unverified placeholders). Must query accurate version numbers before outputting code.

### 2.2 Client SDK

Official client SDKs are available for iOS, Android, and HarmonyOS. Use the document search tool to find specific SDK download URLs and integration guides.

#### 2.2.1 Native APP Payment Priority

- When the user's goal is to integrate APP payment into an existing iOS/Android/HarmonyOS native project, prioritize the native client SDK integration flow
- When APP SDK integration scenario is triggered, first read `references/app-sdk-integration-guide.md`
- `references/app-sdk-integration-guide.md` is the sole source of truth for APP SDK integration; this file no longer maintains platform-specific details

#### 2.2.2 Client Minimum Red Lines

- Clients do NOT generate signature logic; only consume complete payment invocation parameters returned by the server
- Client results are for reference only; final payment status is determined by server notification/query
- All existing payment implementations should be referred to as "third-party payment"; do not proactively output any specific brand names
- Generated integration code must maintain the same complexity as official doc examples — if the docs don't include logic, the code shouldn't either

### 2.3 Development Language & Standards Confirmation

- When the user hasn't specified, prefer the language already used in their existing project
- Follow the user's development standards requirements (if any)

### 2.4 Languages Without Official SDK

For languages without an official SDK:

- Use HTTP protocol to call DouyinPay API directly
- Must implement signing and verification logic yourself
  - How to construct request signatures: https://pay.douyinpay.com/wiki/66aa57118a7da602efb9bc2f/66aa574888f38d02f9de20da
  - How to verify signatures: https://pay.douyinpay.com/wiki/66aa57118a7da602efb9bc2f/66aa57df29710302ee143340
- Can use `terminal scripts/gen_rsa_key.sh` to generate key pairs

## III. Typical Coding Flow

### 3.1 Server Integration Flow (Go Example)
1. Install SDK → `go get github.com/douyinpay/douyinpay-go`
2. Initialize client (configure AppID, merchant ID, private key path)
3. Construct order request (product info, amount, callback URL)
4. Call order creation API
5. Handle order response (get payment parameters/QR code link)
6. Implement callback endpoint (verify → business processing → return success)
7. Implement order query API (optional, for active polling)
8. Implement refund API (optional)

### 3.2 Client Integration Flow (APP Payment Example)

When the client APP payment integration scenario is triggered, **must** first read `cat references/app-sdk-integration-guide.md` and follow its complete workflow.

---

## IV. Common Considerations

### 4.1 Signature

- Signature algorithm: RSA-SHA256
- Private key format: PKCS#8 PEM (generated by `terminal scripts/gen_rsa_key.sh`)
- Signature string concatenation order must strictly follow documentation; field order is immutable
- Verification uses the DouyinPay public key certificate; remind the user to download from the merchant platform

### 4.2 Callback Handling

- Callback URL must be a publicly accessible HTTPS address
- **Verification & Decryption**: Upon receiving a callback, first verify the signature in the HTTP headers; the notification data (`resource`) is AES-256-GCM encrypted and must be decrypted using the API encryption key to get the plaintext business data. After callback verification, perform "element consistency check": order number, amount, merchant ID/app ID must match the local order
- **Response Requirements**: On successful processing, **must return HTTP status 200 or 204 with an empty body**; on error, return 4xx or 5xx with a response body
- **Idempotency & Concurrency Control**: Same order callbacks may arrive duplicated or out of order; the merchant system must support idempotent processing. Use **data locks for concurrency control** when checking/updating business data state to avoid function re-entry and duplicate accounting/shipping
- **Retry Mechanism**: If the response times out or returns non-200/204, DouyinPay will retry per policy (15s to 6h intervals, totaling approximately 24 hours 4 minutes)
- **Must have "callback miss fallback"**: If no callback is received for an extended period, implement scheduled tasks or manual order query to avoid stuck orders

### 4.3 Amount Handling

- API amount unit is **cents** (integer), e.g. 1 yuan = 100
- Ensure amount type is integer; do not pass floating point numbers
- Convert to yuan for frontend display, paying attention to **floating point precision issues**
- For reconciliation unit conversion: if bill amount units differ from API amount units, unify units before reconciliation

### 4.4 Common Error Codes

| Code | Common Cause | Troubleshooting |
|------|-------------|-----------------|
| 401 | Signature error / Invalid certificate | Check private key correctness and signature string concatenation |
| 400 | Missing or malformed parameters | Check required parameters and types |
| 403 | Insufficient permissions | Check if the product is enabled for the merchant |
| 500 | Server internal error | Retry later; contact support if persistent |

For other errors, use `search_docs` tool (`terminal scripts/search_docs.sh "error info"`) to search for relevant information.

### 4.5 Code Standards

#### Frontend Code Standards

- Separate js/ts/tsx, css, and html; avoid inline style and onclick where possible
- Prefer TypeScript for frontend
- Do not introduce build tools unless necessary

#### Backend Code Standards

- Go: prefer html/template + htmx
- PHP: prefer Blade/Twig + htmx
- Java: prefer Thymeleaf + htmx

## V. Code Generation Behavior Standards (Mandatory)

### 5.1 Output Format & Completeness

- **Demo Mode (run from scratch)**: When the user provides an empty directory or explicitly requests a runnable demo project, output must include all runnable code files (no Markdown guides as code substitute). If both client and server code are included, they must be in the same project directory.
- **Existing Project Mode (minimal invasive modification)**: When the user provides an existing project, default to minimal modifications within the existing project structure; do not generate standalone demos or large refactors. If insufficient information prevents safe implementation, ask for confirmation about target module/files before proceeding.
- **Certificates & Keys**: If the user hasn't generated merchant private key/CSR, call `terminal scripts/gen_rsa_key.sh --out-dir ./certs` (RSA only) and remind the user to upload CSR to apply for merchant public key certificate (RSA); if the user already has certificates/keys, do not overwrite existing files.

### 5.2 Client APP Payment Output Constraints

Client APP payment output constraints are unified in the following file:

- `cat references/app-sdk-integration-guide.md`

### 5.3 Client Configuration Output Template

Configuration output template is unified in the following file:

- `cat references/app-sdk-integration-guide.md`

## VI. Integration Testing

To ensure secure and smooth DouyinPay launch:

- DouyinPay currently does not provide a test or sandbox environment; be aware of fund safety (all deductions, settlements are in production environment)
- For demo or test scenarios, order amounts should be the minimum 1 cent
- H5 payment and JSAPI payment require configuring H5 payment domain and JSAPI payment authorization directory on the Merchant Platform - Product Center - Development Configuration first
- Before calling the split-account request API, you must first add split-account recipients
