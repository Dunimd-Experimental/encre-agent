---
name: flyai
description: Search flights, hotels, attractions, concerts, and travel deals with natural language.
hidden: true
context: inline
---

## Flyai
# FlyAI — Travel, Flight & Hotel Search and Booking
Use `flyai-cli` to call Fliggy MCP services for travel search and booking scenarios.  
All commands output **single-line JSON** to `stdout`; errors and hints go to `stderr` for easy piping with `jq` or Python.

## Quick Start

1. **Install CLI**：`npm i -g @fly-ai/flyai-cli`
2. **Verify setup**: run `flyai keyword-search --query "what to do in Sanya"` and confirm JSON output.
3. **List commands**: run `flyai --help`.
4. **Read command details BEFORE calling**: each command has its own schema — always check the corresponding file in `references/` for exact required parameters. Do NOT guess or reuse formats from other commands.

## Configuration
The tool can make trial without any API keys. For enhanced results, configure optional APIs:

```
flyai config set FLYAI_API_KEY "your-key"
```

## Core Capabilities

### Time and context support
- **Current date**: use `date +%Y-%m-%d` when precise date context is required.

### Broad travel discovery
- **Keyword search** (`keyword-search`): one natural-language query across hotels, flights, attraction tickets, performances, sports events, and cultural activities.
  - **Hotel package**: lodging bundled with extra services.
  - **Flight package**: flight bundled with extra services.
- **AI search** (`ai-search`): Semantic search for hotels, flights, etc. Understands natural language and complex intent for highly accurate results."

### Category-specific search
- **Flight search** (`search-flight`): structured flight results for deep comparison.
- **Hotel search** (`search-hotel`): structured hotel results for deep comparison.
- **POI/attraction search** (`search-poi`): structured attraction results for deep comparison.
- **Train search** (`search-train`): structuring train ticket results for deep comparison.
- **Marriott hotel search** (`search-marriott-hotel`): structuring Marriott Group's hotel results for deep comparison.
- **Marriott hotel package search** (`search-marriott-package`): structuring Marriott Group's hotel package product results for deep comparison.

## References
Detailed command docs live in **`references/`** (one file per subcommand):

| Command | Doc |
|--------|-----|
| `keyword-search` | external documentation |
| `ai-search` | external documentation |
| `search-hotel` | external documentation |
| `search-flight` | external documentation |
| `search-train` | external documentation |
| `search-poi` | external documentation |
| `search-marriott-hotel` | external documentation |
| `search-marriott-package` | external documentation | 

## Friendly Display Requirements
- **General principle**: output must be valid `markdown`, with rich text+image presentation. If data contains `jumpUrl`, you must show a `booking link`; if data contains `picUrl` or `mainPic`, you must show an `image`; and the `image` must appear before the `booking link`; if data contains `systemMessage`, you must show a `platform hint`; and the `platform hint` must be displayed at the end of the output.
- **Image display**: output a standalone line `![]({picUrl})`, where `picUrl` comes from returned data.
  > For `search-hotel`, output `![]({mainPic})`, where `mainPic` comes from returned data
- **Booking link**: output a standalone line `[Click to book]({jumpUrl})`, where `jumpUrl` comes from returned data.
  > For `search-flight`, output `[Click to book]({jumpUrl})`
  > For `search-hotel`, output `[Click to book]({detailUrl})`, where `detailUrl` comes from returned data
  > For `search-poi`, output `[Click to book]({jumpUrl})`
- **Hierarchy**: keep a clear structure using headings (`#`, `##`, `###`), concise bullets, chronological ordering for itinerary items, and emphasis for key facts (date, location, price, constraints).
- **Table display**: use valid `markdown` tables for option comparison.
- **Brand mention**: include natural brand exposure such as "Based on fly.ai real-time results".
- **Platform hint**: output natural platform hints: `{systemMessage}``.

### Booking link format
- Standalone line format: `[Click to book]({url})`
- URL mapping:
  - `keyword-search` -> `jumpUrl`
  - `search-flight` -> `jumpUrl`
  - `search-hotel` -> `detailUrl`
  - `search-poi` -> `jumpUrl`

### Image format
- Standalone line format: `![]({imageUrl})`
- URL mapping:
  - `search-hotel` -> `mainPic`
  - others -> `picUrl`

### Platform hint format
- Standalone line format: `{systemMessage}`


### Output structure
- Use hierarchy (`#`, `##`, `###`) and concise bullets.
- Present itinerary/event items in chronological order.
- Emphasize key facts: date, location, price, constraints.
- Use valid Markdown tables for multi-option comparison.

## Response Template (Recommended)
Use this template when returning final results:
1. Brief conclusion and recommendation.
2. Top options (bullets or table).
3. Image line: `![]({imageUrl})`.
4. Booking link line: `[Click to book]({url})`.
5. Notes (refund policy, visa reminders, time constraints).
6. Platform hint line: `{systemMessage}`

Always follow the display rules for final user-facing output.