# APP SDK Integration Master Reference (Client)

> This file is a flow orchestration and retrieval verification reference for APP SDK integration, not the official source of truth. `SKILL.md` retains only entry points/blocking checkpoints/clarification order and capability boundaries; `coding-guide.md` retains only common coding constraints.
> For SDK version numbers, API names, method signatures, configuration items, and download URLs, the official Markdown source obtained at runtime is the sole authority.

## 0. Execution Order Quick Reference (Anchors)

`Read official docs` → `§2bis Third-party read-only identification` → `Complete clarification per Section 3` → `Echo matched dimensions` → `Execute per Sections 5-11 (placement per §11.2 Decision Table)` → `Output per §12.1 Four-category archive`

Any step hitting a hard block in Section 4 pauses immediately; hitting a soft block switches to minimal invasive scheme with explicit declaration.

## 1. Scope

Use this file when the user needs to integrate DouyinPay client SDK in an iOS / Android / HarmonyOS native project involving any of the following:

- SDK integration (package manager / offline package)
- Invoking payment
- Handling payment result callbacks
- iOS `Scheme` / `Universal Links` integration branch
- Referencing existing third-party payment code organization for placement

Compliance requirements:

- When the Skill proactively outputs, all existing payment implementations should be referred to as "third-party payment"; do not proactively output any specific brand name
- When the user provides code, logs, or file paths containing specific brand names, they may be retained verbatim within the context of referencing user facts, but no new brand-related descriptions may be derived from them
- Section 11.1 identification anchors are for internal Skill-side judgment only; when echoing results to users, omit brand keywords and only echo "dependency coordinates, file path, matched anchor category"

## 2. Document Reading (Fixed URLs for Latest Docs)

Get the latest full document content. Do not proceed to Section 3 clarification or Section 5 execution until documents are read.

Must use `get_full_doc` for the following 3 fixed URLs to obtain official Markdown source. Do not rely solely on local reference files or search summaries.

Fixed document URLs:

1. OpenSDK version info document
   - `https://pay.douyinpay.com/wiki/639fd757f4a57b0226bb2066/668fcf3699cc6202f1bfb0ef`
2. OpenSDK integration document
   - `https://pay.douyinpay.com/wiki/639fd757f4a57b0226bb2066/639fd78317c2f3021d238378`
3. App payment invocation document
   - `https://pay.douyinpay.com/wiki/639fd48f17c2f3021d237f61/639fd5e470f838021f2961e5`

Requirements:

1. If reading fails, stop immediately and inform the user of the failed URL
2. After success, echo the actual document URLs used and the update time or version number (if extractable from the body)

Fact source rules:

1. All subsequent version numbers, APIs, configuration items, download URLs, and judgment criteria for clarifying third-party payment integration methods may only be extracted from the documents obtained at runtime
2. Local reference documents are for flow orchestration only; in case of conflict with official documents, official documents take precedence

## 2bis. Third-Party Payment Read-Only Identification (Pre-Clarification)

After Section 2 document reading completes and before entering Section 3 clarification, perform one low-cost read-only identification to:

- Provide evidence for clarification item 3 (whether third-party payment is already integrated)
- Provide candidate sorting basis for clarification item 5 (target files)

Allowed file read scope strictly equals §12.2 Capability Boundaries, no expansion:

- iOS: `Podfile`, `Podfile.lock`, `Info.plist`, `*.xcodeproj/project.pbxproj`
- Android: `settings.gradle(.kts)`, root and module-level `build.gradle(.kts)`, `AndroidManifest.xml`
- HarmonyOS: `oh-package.json5`, `module.json5`

Identification actions:

1. Extract suspected payment SDK dependencies from dependency manifests (per §11.1 identification anchors)
2. Extract suspected payment responsibility point files from source index/configuration (per §11.1 identification anchors)
3. Organize results as structured echo:
   - Suspected third-party payment channel list (dependency coordinates + source file)
   - Payment responsibility point candidates (file path + matched anchor category)
   - Clearly note: "Above are read-only identification results; please confirm before proceeding"

Identification rules:

- Read-only identification; do not make any changes, downloads, or persistent writes
- If 0 items identified, directly note "No third-party payment traces identified"; do not further question about channels
- If multiple items identified, present per §11.2 Decision Table candidate sorting rules
- If identification fails or requires files beyond capability boundaries, handle per Section 4 blocking checkpoints
- This section's output does not replace user confirmation in Section 3; it only serves as context for confirmation questions

## 3. Pre-Integration Clarification (Ordered)

This phase only collects user confirmation; do not perform any downloading, configuration, file writes, code generation, or scheme placement.
When any clarification item lacks explicit user confirmation, handle per Section 4 blocking checkpoints.

Clarification items:

1. **Platform confirmation**: iOS / Android / HarmonyOS
2. **Integration method confirmation**: Package manager / Offline package
3. **Third-party payment integration confirmation**
   - First echo the §2bis read-only identification results
   - If suspected third-party payment channels identified: ask "Are the above channels valid third-party payments in your project? Are there any channels not identified?"
   - If none identified: ask "Is it confirmed that no third-party payment is integrated?"
   - Do not proceed to execution until the user answers
4. **Target module / target confirmation**: Must confirm ownership when multiple candidates exist
5. **Target file confirmation**: Used for third-party payment placement reference
   - iOS: See Section 11
   - Android / HarmonyOS: Do not select target files by default
   - Candidate list must be based on §2bis read-only identification results, sorted per §11.2 Decision Table sorting rules
   - Read-only identified candidates count as a blocking checkpoint per Section 4 until explicitly confirmed by the user
6. **iOS callback method confirmation** (only when platform is iOS; skip for Android/HarmonyOS)
   - If the user has explicitly indicated Universal Links: ask for the complete Universal Link URL
   - If the user hasn't specified: ask "Scheme or Universal Links"

## 4. Blocking Checkpoints (Global Single Source of Truth)

This section is the sole source of truth for stop rules throughout the flow. When other sections mention "stop/block/wait for confirmation", refer back to this section.
Blocks are two-tiered: **Hard block** pauses immediately and waits for user confirmation; **Soft block** switches to minimal invasive scheme and explicitly declares in output.

### 4.1 Hard Block (Pause on any hit; do not infer and continue)

1. Any required clarification item missing or not yet confirmed by user; or user hasn't explicitly agreed to proceed to execution
2. Read-only identified candidates not yet explicitly confirmed by user
3. When **structural changes** are needed (definition in §11.3):
   - Modifying existing third-party payment dispatcher/registry signatures or protocols
   - Replacing or migrating existing payment entry points
   - Deleting key code or configuration of existing payment channels
4. User-provided information conflicts with code reality and cannot be reconciled
5. Key document read failure, content conflict, or insufficient to support unique judgment after reading
6. Platform final product does not match current platform (download verification failure in Section 7)

### 4.2 Soft Block (Switch to minimal invasive scheme; do not stop)

1. Multiple modules, targets, abilities, entry files, or placement file candidates:
   - Sort per §11.2 Decision Table and present to user; do not select by default, but allow generating products that don't depend on specific placement (e.g. standalone util/tool class code)
2. Project structure doesn't exactly match doc examples but involves **additive changes** (definition in §11.3):
   - Follow existing project style; output diff/patch fragments instead of full replacements
3. Adjusting existing integration approach without touching third-party payment core protocols:
   - Allow to continue, but must note "List of files affected by this change" in output and have user confirm

Soft block output must explicitly declare: "Below is a minimal invasive scheme based on <basis>. Please confirm before merging."

## 5. Execution Phase (Multi-dimensional Combination, Not Single Branch)

Only enter execution phase when Section 2 document reading is complete, Section 3 clarification is complete, and Section 4 blocking checkpoints are not hit.

Core rules:

1. Final execution path = combined result of all dimensions triggered by this task, not a single choice among branches
2. Upon entering execution phase, first echo all triggered dimensions, then proceed with the combination
3. If execution reveals new dimensional information that affects the existing scheme, return to Section 3 for re-clarification

Execution dimensions:

1. Integration method dimension: Package manager / Offline package
2. Platform dimension: iOS / Android / HarmonyOS
3. Project status dimension: Third-party payment integrated / Not integrated
4. iOS callback dimension (iOS only): Scheme / Universal Links

## 6. Integration Method Dimension: Package Manager

### 6.1 Version Echo Rules

When the user chooses package manager integration:

1. First read the OpenSDK version info document
2. After successful reading, echo to the user only:
   - Source document
   - Current reference version
   - Final dependency configuration

### 6.2 Dependency Syntax (Per Official Docs)

The following only serves as platform difference hints. Final syntax defers to runtime documentation:

- iOS: Per doc's CocoaPods source configuration and dependency declaration
- Android: Per doc's Maven repository and dependency declaration
- HarmonyOS: Per doc's ohpm source configuration and dependency declaration

## 7. Integration Method Dimension: Offline Package (Into Project Directory)

Prerequisites: Platform, target module/target, in-project directory, and user consent to execute all confirmed.
If any item unconfirmed, handle per Section 4 blocking checkpoints.

Download rules:

1. Get download URL for the corresponding platform from the "4. Offline Package Download" section of the official client SDK integration doc
2. Must extract download info from `<a>` tags in the document source: `href` as the real download URL; if `filename="..."` exists, prefer as save filename
3. If download link displays as `...image.image` or other non-standard suffix, do not judge failure by URL suffix alone
4. If multiple candidate attachments exist for the same platform and cannot be uniquely determined, block per Section 4
5. Download directly to the user-confirmed in-project directory; do not download to system download directory first. `Frameworks/`, `target module/libs/`, etc. can only serve as candidate directories
6. After download, echo source document URL, real download URL, save filename, and final absolute path within the project
7. If extraction is needed, do it within the target project directory; clean up archive and intermediate directories afterward, keeping only the final referenced product

Platform product processing and verification:

1. iOS: After zip extraction, keep only `xcframework`
2. Android: Keep `aar` as final product
3. HarmonyOS: Keep `har` as final product
4. Must verify whether the product is a genuine binary using response headers or file content; do not rely solely on URL suffix
5. If final product doesn't match the current platform, or no product is obtained, treat as failure; do not generate reference code or project configuration; output failure reason and next-step suggestions

## 8. Platform Dimension: iOS (Base Config / Scheme Branch / Universal Links Branch)

### 8.1 iOS Base Configuration (Required for Both Branches)

First, based on the already-read "Client SDK Integration Guide", compile an **iOS required configuration checklist**, covering at minimum:

1. `Info.plist` configuration, e.g. `LSApplicationQueriesSchemes`, `CFBundleURLTypes`
2. System libraries and framework check items
3. `Build Settings` / `entitlements` and other configs explicitly mentioned in the docs

Only process based on the official document content read at runtime; do not preset fixed lists in the skill.

Processing flow:

1. First extract complete checklist from docs
2. Then classify into two categories by "whether stable file write is possible":
   - Repository files like `Podfile`, `Info.plist`, `entitlements` — when target ownership is clear, modify directly; classify as `I have auto-completed`
   - System libraries and frameworks in Xcode `Link Binary With Libraries`, `Frameworks, Libraries, and Embedded Content` — do not auto-modify `project.pbxproj`; classify as `Please complete manually`
3. If multiple targets exist and safe ownership determination is not possible, block per Section 4

Supplement output by integration method:

- **CocoaPods integration**: First complete the doc-required `Podfile` source configuration and dependency declaration; organize doc-required system libraries/frameworks as manual check list for user
- **Offline package integration**: Complete local SDK reference configuration; also organize doc-required system libraries/frameworks as manual check list for user

### 8.2 iOS Scheme Branch

When generating code, extract from the already-read official docs:

1. SDK class name and header import method (from "Client SDK Integration Guide" - "Register APPID" section)
2. Registration API complete signature — Scheme mode
3. callbackScheme value method (registration example code)
4. Payment invocation API complete signature and parameter assembly method (from "App Payment Invocation" - "Invoke DouyinPay" section)
5. Scheme result callback API complete signature and lifecycle method location (from "App Payment Invocation" - "SDK Result Callback" section)

Lifecycle method coexistence rules:

- If §2bis identifies `application:openURL:options:` or `scene:openURLContexts:` with existing third-party payment dispatch logic:
   - **Do NOT output full method examples**; output diff-style patch instead: only add DouyinPay judgment branch
   - The judgment branch only calls the result callback API extracted in item 5 above
   - Note in comment: "This branch sits alongside existing third-party payment branches; existing implementation is not modified"
- If the target method doesn't exist: generate full example per items 1-5

Output must cover: registration, payment invocation, callback handling — 3 action points.

### 8.3 iOS Universal Links Branch

Differences from Scheme branch:

1. Must first collect the complete Universal Link URL and parse out the domain (for applinks configuration)
2. UL mode registration API complete signature (from "Client SDK Integration Guide" - "Using Universal Links to Return to Merchant APP" section)
3. UL result callback API complete signature and lifecycle method location (including both AppDelegate and SceneDelegate variants)
4. Fallback requirement: Even with UL, must retain callbackScheme and return scheme configuration for low-version downgrade

Lifecycle method coexistence rules:

- If §2bis identifies `application:continueUserActivity:restorationHandler:` or `scene:continueUserActivity:` already occupied by existing third-party payment:
   - Output diff-style patch: only add DouyinPay UL callback branch judged by `host / path`
   - Keep existing third-party payment UL branch unchanged
- If method doesn't exist: generate full example per items 1-4 (including both AppDelegate and SceneDelegate variants)

Output must cover: registration, payment invocation, callback handling — 3 action points.

## 9. Platform Dimension: Android

When generating code, extract from the already-read official docs:

1. Maven repository URL and dependency declaration (from "Client SDK Integration Guide" - Android SDK Integration section)
2. Payment invocation API complete signature, parameter assembly method, and callback interface (from "App Payment Invocation" - Android payment section, including both Kotlin and Java variants)
3. Douyin availability detection API (from "Client SDK Integration Guide" - Capability Detection section)
4. Result callback data structure (from "App Payment Invocation" - SDK Result Callback section)

## 10. Platform Dimension: HarmonyOS

When generating code, extract from the already-read official docs:

1. ohpm source configuration and dependency declaration (from "Client SDK Integration Guide" - HarmonyOS SDK Integration section)
2. Required configuration items in `module.json5`
3. Payment invocation API complete signature (from "App Payment Invocation" - HarmonyOS payment section)
4. Douyin availability detection API

## 11. Project Status Dimension: Third-Party Payment Reference Strategy

If the user has already integrated third-party payment, prefer referencing their existing payment code organization rather than forcing standard templates or presuming architectural patterns.

Notes:

1. Third-party payment projects may have multiple layers of encapsulation and business customization (unified entry, dispatch center, registry, factory, adapter layer, etc.). This file makes no assumptions about their structure.
2. The Skill's goal is to "integrate within the existing project structure," not to refactor the user's existing third-party payment architecture for DouyinPay integration.

Collection and placement principles:

1. iOS: Collect and place by "responsibility points" rather than "fixed file types" (responsibility points may overlap into the same file):
   - Payment invocation responsibility point
   - Registration responsibility point
   - Result handling responsibility point
   Lifecycle entry files serve only as线索; do not assume they are the sole placement point.

2. Android / HarmonyOS: Only perform read-only identification and compile candidate files (typically payment entry or dispatch center-related files); do not select any candidate file as the final placement point by default.

Stop conditions are uniformly handled per Section 4 blocking checkpoints. If changes would touch large-scale refactoring or affect existing third-party payment architectural stability, switch to providing minimal invasive scheme suggestions and have the user confirm before continuing.

### 11.1 Identification Anchor List (Shared by §2bis and §11)

The following anchors are for internal Skill-side identification only; **do not mention any specific third-party payment brand names in user output** (§1 compliance requirements).

#### iOS Identification Anchors

Dependency manifests (`Podfile` / `Podfile.lock`):

- Matching payment-related pod name keywords: `Pay`, `Payment`, `Cashier`, `Checkout`, etc.
- Matching payment brand-prefixed pods (identified by pod name string matching; do not echo brand name in output)

Source code (read-only `*.xcodeproj/project.pbxproj` file index; do not read source file content):

- File names containing `Pay`, `Payment`, `Cashier`, `Checkout`, `PayManager`, `PayService`, `PayChannel`, `PayRouter`, etc.
- `AppDelegate.swift/m/mm`, `SceneDelegate.swift` as lifecycle entry candidates

#### Android Identification Anchors

Dependency manifests (root and module `build.gradle(.kts)`):

- `implementation` / `api` lines containing payment-related artifact keywords

Source index:

- File names containing `Pay`, `Payment`, `Cashier`, `Checkout`, `PayManager`, `PayService`, `PayChannel`, `PayDispatcher`, etc.

Configuration (`AndroidManifest.xml`):

- `<activity>` with `*.pay.*` callback activity declarations
- intent-filter scheme matching payment-related prefixes

#### HarmonyOS Identification Anchors

Dependency manifests (`oh-package.json5`):

- `dependencies` containing payment-related HAR name keywords

Configuration (`module.json5`):

- `abilities` with names containing `Pay` / `Payment` / `Cashier` keywords
- `skills` uri scheme matching payment-related prefixes

#### Cross-Platform Responsibility Point Classification

- **Payment invocation responsibility point**: File containing SDK `pay` / `startPay` / `sendReq` API calls
- **Registration responsibility point**: File containing SDK `register` / `init` / `setXxxAppId` API calls
- **Result handling responsibility point**: File implementing SDK callback protocol/interface/listener

Three responsibility point types may overlap into the same file (see §11.2 Decision Table).

### 11.2 Placement Decision Table

Based on §11.1 identification results, determine DouyinPay code placement as follows:

| Responsibility Distribution | Entry Status | Placement Behavior | Block Level |
|---|---|---|---|
| All three overlap in one "unified entry/dispatcher" file | Unique | Append DouyinPay branch in same file; no new file | Direct execute |
| Three spread across multiple files, clear individual responsibility | Each unique | Append to corresponding file per responsibility | Direct execute |
| Responsibility points exist but multiple candidate files | Multiple candidates | Present sorted to user for confirmation | Soft block (§4.2) |
| Third-party dependency identified but no responsibility point located | Unknown | Ask user to specify target file, or suggest new file at same level | Soft block (§4.2) |
| No third-party payment identified | None | Standard placement per platform doc examples; don't force abstraction | Direct execute |
| Dispatcher protocol/signature modification needed for integration | Any | Output "suggested refactor plan + impact scope" | Hard block (§4.1) |

Candidate file sorting rules (for "multiple candidates" scenarios):

1. Higher responsibility point overlap preferred
2. More anchor keyword matches in filename preferred
3. Direct reference relationship with lifecycle entry (AppDelegate, etc.) preferred
4. Same rank sorted alphabetically by path

### 11.3 Additive vs. Structural Change Determination

Used for §4 block grading and §11.2 Decision Table unified judgment.

**Additive change** (soft block or direct execute):

- Only add new files; do not modify existing file export symbols
- When modifying existing files, only append; do not delete/rename existing methods, protocols, constants
- Add new cases/branches in existing branch statements; preserve original branch behavior
- Append one dependency line to existing dependency manifest; do not upgrade/downgrade existing dependency versions

**Structural change** (hard block):

- Modify existing third-party payment dispatcher/registry signatures, protocols, registration order
- Delete or rename existing payment entry points, callback methods
- Upgrade/downgrade existing third-party payment SDK versions
- Refactor single-channel dispatch to multi-channel routing (requires user authorization)

Minimal invasive scheme = smallest set of "additive changes" to achieve DouyinPay integration goals.

## 12. Cross-Cutting Rules

This section does not participate in path selection but applies continuously across all execution paths. Priority order:

1. Section 4 blocking checkpoints have highest priority
2. User-confirmed clarification results take precedence over default examples and candidates
3. Sections 5-11 execution dimensions determine the current execution path
4. This section constrains output format and code generation approach

### 12.1 Output Template (Four Categories)

Output is uniformly divided into four categories:

1. `I have auto-completed`: In-project, in-repo, stable file write possible
2. `I have generated for you`: Produces artifacts needing user hosting/upload/platform configuration
3. `I need you to supplement before auto-completing`: Theoretically auto-completable, but current target/module ownership unclear, too many candidates, or structural anomalies prevent safe file write; also includes cases under §4.2 soft block where minimal invasive scheme is generated but needs user confirmation for placement
4. `Please complete manually`: Out-of-repo platform backend, domain/AASA hosting, or agent cannot perform

Different artifacts from the same task may be classified into different categories. If any artifact changes from "auto-completable" to "ownership unclear or risk elevated" during execution, reclassify as `I need you to supplement before auto-completing`.

### 12.2 Capability Boundaries

1. Only allow one low-cost read-only scan (iOS: `Podfile`, `Info.plist`, `.xcodeproj`; Android: `settings.gradle`, `build.gradle`, `build.gradle.kts`; HarmonyOS: `oh-package.json5`, `module.json5`); items beyond scope require user clarification
2. iOS/UL requires user to provide complete Universal Link; AASA hosting is out-of-repo and classified as `I have generated for you` / `Please complete manually`

### 12.3 Code Generation Principles

1. Client does not generate signature logic; only consumes complete payment invocation parameters returned by the server
2. Payment invocation parameter examples should **directly follow the official "App Payment Invocation" page examples**: use dictionary/Map literals listing fields, fill in example values and Chinese comments; also note in comments: "All parameters above are returned by the server pre-order API; the client does not perform signature computation"
3. Client results are for reference only; final payment status is determined by server notification/query
4. Prefer modifying user's existing native project files; do not generate standalone client demo or bridging projects
5. Generated integration code must maintain same complexity as official doc examples — if docs show direct calls, use direct calls; if docs don't have logic, code shouldn't either
6. If user's project already has a third-party payment module, follow its existing style; with no reference, do not proactively create abstraction layers
