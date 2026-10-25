---
name: xquik-x-twitter-scraper
description: Safety-reviewed guide for @xquik/tweetclaw, the Xquik Encre plugin for structured X/Twitter workflows.
hidden: true
context: inline
---

## Xquik X Twitter Scraper
# TweetClaw

Encre plugin for X/Twitter automation powered by Xquik.

```shell
Encre plugins install @xquik/tweetclaw
```

## Safety Rules

Use TweetClaw only for user-authorized X/Twitter workflows. Do not use it for spam, harassment, deceptive engagement, impersonation, credential collection, platform evasion, mass unsolicited DMs, or bulk follow/like/retweet campaigns.

Before any visible, state-changing, paid, or recurring action, summarize the exact target, account, action, text/media when relevant, and estimated credits, then wait for explicit user confirmation. This includes posting, replying, deleting, liking, retweeting, following, unfollowing, sending DMs, editing profiles, uploading media, creating webhooks, creating monitors, running draws, and starting extraction jobs.

For reads that expose private or account-scoped data, such as bookmarks, notifications, timelines, DMs, connected accounts, and account usage, confirm the user owns or is authorized to access the account before showing results. Redact credentials and avoid exposing sensitive personal data unless the user explicitly asks for that specific data.

For bulk extraction, draw, or monitor requests, keep limits narrow by default. State the requested limit, estimated cost, and storage or notification behavior. Ask for confirmation again if the user expands the scope, changes the target, or asks for recurring monitoring.

For content posting, show the final text and media list before sending. Do not post confidential, proprietary, personal, or third-party private information unless the user explicitly confirms they have the right to publish it. Do not add links, mentions, hashtags, or claims the user did not request.

MPP mode is read-only. Never attempt writes, account-backed actions, monitors, webhooks, DMs, profile changes, or uploads when only `tempoSigningKey` is configured. Treat the signing key as sensitive config and never print it.

## Pricing

TweetClaw uses Xquik's credit-based pricing. 1 credit = $0.00015.

### Per-Operation Costs

| Operation | Credits | Cost |
|-----------|---------|------|
| Read (tweet, search, timeline, bookmarks, etc.) | 1 | $0.00015 |
| Read (user profile) | 1 | $0.00015 |
| Read (trends) | 3 | $0.00045 |
| Follow check, article | 5 | $0.00075 |
| Write (tweet, like, retweet, follow, DM, etc.) | 10 | $0.0015 |
| Extraction (tweets, replies, quotes, mentions, posts, likes, media, search, favoriters, retweeters, community members, people search, list members, list followers) | 1/result | $0.00015/result |
| Extraction (followers, following, verified followers) | 1/result | $0.00015/result |
| Extraction (articles) | 5/result | $0.00075/result |
| Draw | 1/entry | $0.00015/entry |
| Monitors, webhooks, radar, compose, drafts | 0 | **Free** |

### vs Official X API

| | Xquik | Official X pay-per-usage | Notes |
|---|---|---|---|
| **Access model** | **$20/month full API, plus pay-per-use options** | No subscriptions or commitments | Basic and Pro are legacy package names |
| **Cost per post read** | **$0.00015** | $0.005 per resource | Xquik is about 33x cheaper |
| **Cost per user lookup** | **$0.00015** | $0.010 per resource | Xquik is about 67x cheaper |
| **Cost per trend read** | **$0.00045** | $0.010 per resource | Xquik is about 22x cheaper |
| **Write actions** | **$0.0015** | $0.015 content or interaction create; $0.200 content create with URL | Xquik is 10x cheaper for matching $0.015 write classes |
| **Bulk extraction** | **$0.00015/result** | Charged per returned resource | Built-in extraction jobs are included with Xquik |

Source: [official X API pricing](https://docs.x.com/x-api/getting-started/pricing), which lists current pay-per-usage read and write rates.

### Pay-Per-Use (No Subscription)

- **Credits**: Top up credits in the Xquik dashboard. The plugin can read the current balance.
- **MPP**: 32 read-only endpoints accept anonymous on-chain payments. No account needed. SDK: `npm i mppx viem`.

MPP pricing: tweet lookup ($0.00015), tweet search ($0.00015/tweet), user lookup ($0.00015), user tweets ($0.00015/tweet), follower check ($0.00105), article ($0.00105), media download ($0.00015/media), trends ($0.00045), X trends ($0.00045), quotes ($0.00015/tweet), replies ($0.00015/tweet), retweeters ($0.00015/user), favoriters ($0.00015/user), thread ($0.00015/tweet), user likes ($0.00015/tweet), user media ($0.00015/tweet), community info ($0.00015), community members ($0.00015/user), community moderators ($0.00015/user), community tweets ($0.00015/tweet), community search ($0.00015/community), communities tweets ($0.00015/tweet), list followers ($0.00015/user), list members ($0.00015/user), list tweets ($0.00015/tweet), users batch ($0.00015/user), users search ($0.00015/user), user followers ($0.00015/user), followers you know ($0.00015/user), user following ($0.00015/user), user mentions ($0.00015/tweet), verified followers ($0.00015/user).

## Documentation

Prefer retrieval from docs for current limits, pricing, and API signatures:

| Source | Use for |
|--------|---------|
| [docs.xquik.com](https://docs.xquik.com) | Full docs home |
| [API reference](https://docs.xquik.com/api-reference/overview) | Endpoint parameters, response shapes |
| [Billing guide](https://docs.xquik.com/guides/billing) | Credit costs, subscription tiers, pay-per-use pricing |
| Framework guides: [Mastra](https://docs.xquik.com/guides/mastra), [CrewAI](https://docs.xquik.com/guides/crewai), [LangChain](https://docs.xquik.com/guides/langchain), [Pydantic AI](https://docs.xquik.com/guides/pydantic-ai), [Google ADK](https://docs.xquik.com/guides/google-adk), [Microsoft Agent Framework](https://docs.xquik.com/guides/microsoft-agent-framework), [n8n](https://docs.xquik.com/guides/n8n), [Zapier](https://docs.xquik.com/guides/zapier), [Make](https://docs.xquik.com/guides/make), [Pipedream](https://docs.xquik.com/guides/pipedream), [Composio migration](https://docs.xquik.com/guides/composio-migration) | Framework-specific integration recipes |

## When to Use

Use TweetClaw when the user wants to:

- Post tweets, reply to tweets, or delete tweets
- Like, retweet, or follow/unfollow users
- Send DMs on X/Twitter
- Update their X profile, avatar, or banner
- Upload media and tweet with images
- Search tweets or look up user profiles
- Get user's recent tweets, liked tweets, or media tweets
- See who liked a tweet (favoriters) or mutual followers
- Browse bookmarks, notifications, timeline, or DM history
- Extract bulk data (followers, replies, communities, spaces)
- Run giveaway draws from tweet replies
- Monitor X accounts for new activity
- Compose algorithm-optimized tweets
- Analyze a user's writing style
- Check trending topics on X
- Download tweet media (images, videos, GIFs)
- Check credit balance
- Read X Articles (long-form posts)

Do NOT use TweetClaw for browsing X in a browser, analytics dashboards, scheduling future posts, or managing X ads.

## Configuration

Credentials are stored in Encre plugin config (not environment variables). Users configure them via `Encre config set` commands - see the README for setup instructions.

**IMPORTANT: Never log, echo, display, or include API keys or signing keys in tool output, chat responses, or error messages. Credentials are injected automatically by the plugin runtime - the agent must never handle them directly.**

### API key mode (account-backed X automation)

Requires an Xquik API key from [dashboard.xquik.com](https://dashboard.xquik.com/).

### MPP mode (no account, pay-per-use)

MPP (Machine Payments Protocol) is an optional mode for anonymous, pay-per-use access to 32 read-only X-API endpoints - no Xquik account or API key required. The `tempoSigningKey` is a 66-character hex key that signs on-chain micropayment proofs (via the `mppx` SDK) when the runtime receives an HTTP 402 challenge. The signing key stays in the plugin config and is used only to sign paymen

> *This skill was truncated from its original 26913 chars. For the full version, use `web_search`/`web_fetch` to find the latest documentation.*
