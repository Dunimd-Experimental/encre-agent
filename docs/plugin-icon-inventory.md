# 内置技能 / 工具清单与品牌归属

- 总计 **243** 个内置插件（技能 185 · 工具 58）
- 有明确企业/项目品牌归属：**60** 个
- 已使用真实 SVG logo：**67** 个；其余为语义匹配的 lucide 图标
- 所有图标现已统一为**黑白单色**（`#101014`），与白底 tile 的黑字设计一致（彩色品牌标已全部去掉）


## 一、有品牌归属的插件（重点核对）

| # | 插件 | 类型 | 功能 | 品牌 / 归属 | 当前图标 |
|---|---|---|---|---|---|
| 1 | `ea-skill-s3` | skill | Work with S3-compatible object storage with proper security, lifecycle policies, and access patterns. | AWS S3（Amazon） | lucide: hard-drive |
| 2 | `ea-skill-aws-infra` | skill | Chat-based AWS infrastructure assistance using AWS CLI and console context. Use for querying, auditing, and monitoring … | AWS（Amazon） | ✔ 真 logo |
| 3 | `ea-skill-agentmail` | skill | API-first email platform designed for AI agents. | AgentMail（AI 邮件服务） | lucide: inbox |
| 4 | `ea-skill-bibigpt-skill` | skill | BibiGPT CLI for summarizing videos, audio, and podcasts directly in the terminal. | BibiGPT（第三方 B 站/视频 AI 摘要） | lucide: tv |
| 5 | `ea-skill-crypto` | skill | Cryptocurrency market data and price alert monitoring tool based on CCXT. Supports multiple exchanges, real-time price … | Bitcoin / 加密资产（非单一企业） | ✔ 真 logo |
| 6 | `ea-skill-changelog-video` | skill | Turn a weekly changelog .md into a finished branded changelog video (square 1080, ~45-60s, Annie VO, animated brand bac… | Changelog 工作流（内部） | lucide: history |
| 7 | `ea-skill-defuddle` | skill | Extract clean markdown content from web pages using Defuddle CLI, removing clutter and navigation to save tokens. Use i… | Defuddle（开源网页正文提取） | lucide: filter |
| 8 | `ea-skill-exa-web-search-free` | skill | Free AI search via Exa MCP. Web search for news/info, code search for docs/examples from GitHub/StackOverflow, company … | Exa（语义搜索 API） | lucide: search |
| 9 | `ea-skill-pr-to-video` | skill | Turn a GitHub pull request (a PR URL, owner/repo#N, or 'this PR' in a checked-out repo) into a code-change explainer vi… | GitHub PR（Microsoft）（工作流） | lucide: git-merge |
| 10 | `ea-skill-gh-cli` | skill | GitHub CLI (gh) comprehensive reference guide - repositories, issues, pull requests, Actions, projects, releases and mo… | GitHub（Microsoft） | ✔ 真 logo |
| 11 | `ea-skill-goplaces` | skill | Query Google Places API (New) via the goplaces CLI for text search, place details, resolve, and reviews. | Google Places（地点搜索 API） | lucide: map-pinned |
| 12 | `ea-skill-hyperframes` | skill | Mandatory entry point: read this first for any request to make, create, edit, animate, or render a video, animation, or… | HyperFrames（自研） | lucide: frame |
| 13 | `ea-skill-hyperframes-animation` | skill | All animation knowledge for HyperFrames — atomic motion rules, multi-phase scene blueprints, scene transitions, broader… | HyperFrames（自研） | lucide: zap |
| 14 | `ea-skill-hyperframes-cli` | skill | Use the HyperFrames CLI development loop: init, add, catalog, capture, lint, check, snapshot, compare, grade-compare, p… | HyperFrames（自研） | lucide: square-terminal |
| 15 | `ea-skill-hyperframes-core` | skill | The HyperFrames composition contract — build one renderable project. Use for composition structure, the `data-*` timing… | HyperFrames（自研） | lucide: box |
| 16 | `ea-skill-hyperframes-creative` | skill | Non-animation creative direction for HyperFrames videos. Use for design spec (frame.md / design.md) handling, palettes,… | HyperFrames（自研） | lucide: palette |
| 17 | `ea-skill-hyperframes-keyframes` | skill | Use when a HyperFrames composition needs seek-safe 2D/3D keyframes, GSAP timelines, CSS keyframes, Anime.js, WAAPI, FLI… | HyperFrames（自研） | lucide: diamond |
| 18 | `ea-skill-hyperframes-registry` | skill | Install, discover, and wire registry blocks and components into HyperFrames compositions. Use when running hyperframes … | HyperFrames（自研） | lucide: library |
| 19 | `ea-skill-kubectl-skill` | skill | Execute and manage Kubernetes clusters via kubectl commands. Query resources, deploy applications, debug containers, ma… | Kubernetes（CNCF）（开源项目） | ✔ 真 logo |
| 20 | `ea-skill-linkedin` | skill | LinkedIn automation via browser relay or cookies for messaging, profile viewing, and network actions. | LinkedIn（Microsoft） | lucide: share |
| 21 | `ea-skill-outlook-delegate` | skill | Read, search, and manage Outlook emails and calendar via Microsoft Graph API with delegate support. | Microsoft Outlook | lucide: mail-plus |
| 22 | `ea-skill-mcp-builder` | skill | Build high-quality MCP (Model Context Protocol) servers connecting LLMs with external APIs and services, with comprehen… | Model Context Protocol（Anthropic）（开放协议） | lucide: wrench |
| 23 | `ea-skill-gpt-image-1-5` | skill | Generate and edit images using OpenAI's GPT Image 1.5 model. Use when the user asks to generate, create, edit, modify, … | OpenAI（GPT-Image 模型） | lucide: wand-sparkles |
| 24 | `ea-tool-ssh` | tool | Run commands, transfer files, or execute multi-line scripts on a remote host via SSH/SCP using the system ssh and scp c… | OpenSSH（开放标准） | lucide: terminal |
| 25 | `ea-skill-remotion-to-hyperframes` | skill | Port an existing Remotion (React) composition''s source to HyperFrames HTML. Use ONLY on an explicit ask to port/conver… | Remotion + HyperFrames（开源/自研） | lucide: repeat |
| 26 | `ea-skill-slack` | skill | Use when you need to control Slack from Clawdbot via the slack tool, including reacting to messages or pinning/unpinnin… | Slack（Salesforce） | ✔ 真 logo |
| 27 | `ea-skill-slack-gif-creator` | skill | Knowledge and utilities for creating animated GIFs optimized for Slack. Provides constraints, validation tools, and ani… | Slack（Salesforce）（GIF 素材） | lucide: image-play |
| 28 | `ea-skill-slack-integration` | skill | Slack integration - messaging, channel management, conversation search | Slack（Salesforce）（集成/接入） | lucide: plug-zap |
| 29 | `ea-skill-stripemeter` | skill | Integrate Stripe usage-based billing with idempotent event ingestion, late-event handling, and pre-invoice reconciliati… | Stripe | ✔ 真 logo |
| 30 | `ea-skill-deploy-to-vercel` | skill | Deploy applications and websites to Vercel. Use when the user requests deployment actions like "deploy my app", "deploy… | Vercel | ✔ 真 logo |
| 31 | `ea-skill-xquik-x-twitter-scraper` | skill | Safety-reviewed guide for @xquik/tweetclaw, the Xquik Encre plugin for structured X/Twitter workflows. | X（原 Twitter） | ✔ 真 logo |
| 32 | `ea-skill-ezbookkeeping` | skill | Use ezBookkeeping API Tools script to record new transactions, query transactions, retrieve account information, retrie… | ezBookkeeping（开源记账） | lucide: calculator |
| 33 | `ea-skill-product-launch-video` | skill | Turn a product or marketing URL, pasted script, or brief into a product launch / promo video — SaaS promos, feature rev… | 产品发布工作流（内部） | lucide: party-popper |
| 34 | `ea-skill-wecom` | skill | WeCom (Enterprise WeChat) webhook message sending | 企业微信（腾讯） | ✔ 真 logo |
| 35 | `ea-skill-bilibili` | skill | Design videos for cultural resonance on Bilibili. | 哔哩哔哩 Bilibili（视频策划/发布） | ✔ 真 logo |
| 36 | `ea-skill-byted-acep-api` | skill | Manage and troubleshoot Volcano Engine cloud phone resources via local Python CLI and OpenAPI client. Suitable for quer… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: cable |
| 37 | `ea-skill-byted-ark-evolve` | skill | Agent self-evolution system. Collects signals from user feedback, stores execution trajectories (golden + correction pa… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: dna |
| 38 | `ea-skill-byted-ark-trainer` | skill | Large model training task automation tool based on ark_sdk. Helps users create and submit Ark model training tasks thro… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: dumbbell |
| 39 | `ea-skill-byted-las-asr-pro` | skill | Enhanced speech recognition (ASR Pro): Transcribe audio/video to text, supporting enhanced analysis such as noise reduc… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: mic |
| 40 | `ea-skill-byted-las-video-edit` | skill | Smart video editing: Use natural language to describe segments/people/events to keep, automatically extract clips from … | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: scissors |
| 41 | `ea-skill-byted-mediakit-audio` | skill | Audio processing, covering audio processing and enhancement, content understanding, etc. Capabilities include: separate… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: music |
| 42 | `ea-skill-byted-mediakit-editing` | skill | Audio/video editing, covering audio/video concatenation, trimming, compositing, etc. Capabilities include: add-image-to… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: slice |
| 43 | `ea-skill-byted-mediakit-image` | skill | Image processing, covering image compression, image enhancement, AI processing, etc. Capabilities include: image-ocr, e… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: image |
| 44 | `ea-skill-byted-mediakit-shared` | skill | 1. mediakit-cli: supports a variety of operations such as audio/video processing, editing, and images, with some capabi… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: package |
| 45 | `ea-skill-byted-mediakit-video` | skill | Video processing, covering video quality enhancement, video understanding, subtitle erasure, etc. Capabilities include:… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: videotape |
| 46 | `ea-skill-byted-music-generate` | skill | Generate music using Volcengine Imagination API. Supports vocal songs, instrumental BGM, and lyrics generation. Use whe… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: disc |
| 47 | `ea-skill-byted-podcast-gen` | skill | Synthesize summaries of topics or web page content into podcast audio. Generates final audio based on Volcengine Doubao… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: podcast |
| 48 | `ea-skill-byted-seedance-video-generate` | skill | Generate videos using Seedance models. Invoke when user wants to create videos from text prompts, images, or reference … | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: video |
| 49 | `ea-skill-byted-seedream-image-generate` | skill | Generate high-quality images from text prompts using Volcano Engine Seedream models. Supports multiple artistic styles … | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: camera |
| 50 | `ea-skill-byted-sms-sender` | skill | Volcengine SMS service management tool. Use this capability when you need cloud communication features, including sendi… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: message-square |
| 51 | `ea-skill-byted-text-to-speech` | skill | Synthesize text to speech (TTS). Uses Volcengine Doubao Voice Synthesis API, supporting streaming synthesis, multiple t… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: speech |
| 52 | `ea-skill-byted-voice-to-text` | skill | Speech to text (ASR). Uses Volcengine BigModel ASR for speech recognition, including Flash mode (≤2h/100MB synchronous … | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: file-audio |
| 53 | `ea-skill-byted-web-search` | skill | Volcengine web search API, returns web/image results. Prioritize using this skill for web search scenarios. Trigger wor… | 字节跳动 / 火山引擎 ByteDance（byted-* 系列） | lucide: search |
| 54 | `ea-skill-redbookskills` | skill | Xiaohongshu (RED) auto-publish, comment, and content search | 小红书 | ✔ 真 logo |
| 55 | `ea-skill-weixin-connect` | skill | Connect personal WeChat (not Enterprise WeChat). Used when user says \"connect personal WeChat\", \"integrate personal … | 微信（腾讯） | ✔ 真 logo |
| 56 | `ea-skill-douyinpay-payment-integration` | skill | End-to-end DouyinPay payment integration assistant. Provides product selection, preparation guidance, server SDK integr… | 抖音支付（字节跳动） | lucide: credit-card |
| 57 | `ea-skill-douyin-interact-creation` | skill | Create interactive H5 content for Douyin platform that meets submission standards and runs correctly in-app. | 抖音（字节跳动） | ✔ 真 logo |
| 58 | `ea-skill-douyin-interactive-content-publish` | skill | One-click publish tool for Interactive Space. Upload zip+icon to create/update interactive space apps with auto-generat… | 抖音（字节跳动）（内容发布） | lucide: megaphone |
| 59 | `ea-skill-alipay-payment-integration` | skill | Best practice guide for integrating Alipay payment products, covering online and offline payment scenarios. | 支付宝（蚂蚁集团） | ✔ 真 logo |
| 60 | `ea-skill-feishu` | skill | Feishu deep integration skill. Not a simple message bridge, but your digital command center. Designed for the high-pres… | 飞书 Lark（字节跳动） | ✔ 真 logo |

## 二、全部技能（ea-skill-*）

| # | 插件 | 功能 | 品牌 | 图标 |
|---|---|---|---|---|
| 1 | `ea-skill-academic-paper-review` | Use this skill when the user requests to review, analyze, critique, or summarize academic papers, research articles, preprints, o… | — | lucide: graduation-cap |
| 2 | `ea-skill-agentmail` | API-first email platform designed for AI agents. | AgentMail | lucide: inbox |
| 3 | `ea-skill-ai-code-doctor` | This skill performs a two-layer quality and performance audit on Python code, with special focus on code that was written or assi… | — | lucide: stethoscope |
| 4 | `ea-skill-aihotel` | A Skill for searching hotels and querying prices via AIGoHotel MCP (searchHotels / getHotelDetail / getHotelSearchTags) | — | lucide: building |
| 5 | `ea-skill-algorithmic-art` | Creating algorithmic art using p5.js with seeded randomness and interactive parameter exploration. Use this when users request cr… | — | lucide: shapes |
| 6 | `ea-skill-alipay-payment-integration` | Best practice guide for integrating Alipay payment products, covering online and offline payment scenarios. | 支付宝（蚂蚁集团） | ✔ 真 logo |
| 7 | `ea-skill-audio` | Audio file processing - inspect audio info, convert formats, transcribe speech to text, extract audio from video | — | lucide: audio-lines |
| 8 | `ea-skill-aws-infra` | Chat-based AWS infrastructure assistance using AWS CLI and console context. Use for querying, auditing, and monitoring AWS resour… | AWS（Amazon） | ✔ 真 logo |
| 9 | `ea-skill-beautiful-mermaid` | Render Mermaid diagrams as SVG and PNG using the Beautiful Mermaid library. Use when the user asks to render a Mermaid diagram. | — | lucide: workflow |
| 10 | `ea-skill-bibigpt-skill` | BibiGPT CLI for summarizing videos, audio, and podcasts directly in the terminal. | BibiGPT | lucide: tv |
| 11 | `ea-skill-bilibili` | Design videos for cultural resonance on Bilibili. | 哔哩哔哩 Bilibili | ✔ 真 logo |
| 12 | `ea-skill-bootstrap` | - Generate a personalized SOUL.md through a warm, adaptive onboarding conversation. Trigger when the user wants to create, set up… | — | lucide: layout-template |
| 13 | `ea-skill-byted-acep-api` | Manage and troubleshoot Volcano Engine cloud phone resources via local Python CLI and OpenAPI client. Suitable for querying insta… | 字节跳动 / 火山引擎 ByteDance | lucide: cable |
| 14 | `ea-skill-byted-ark-evolve` | Agent self-evolution system. Collects signals from user feedback, stores execution trajectories (golden + correction pairs), anal… | 字节跳动 / 火山引擎 ByteDance | lucide: dna |
| 15 | `ea-skill-byted-ark-trainer` | Large model training task automation tool based on ark_sdk. Helps users create and submit Ark model training tasks through natura… | 字节跳动 / 火山引擎 ByteDance | lucide: dumbbell |
| 16 | `ea-skill-byted-las-asr-pro` | Enhanced speech recognition (ASR Pro): Transcribe audio/video to text, supporting enhanced analysis such as noise reduction and s… | 字节跳动 / 火山引擎 ByteDance | lucide: mic |
| 17 | `ea-skill-byted-las-video-edit` | Smart video editing: Use natural language to describe segments/people/events to keep, automatically extract clips from long video… | 字节跳动 / 火山引擎 ByteDance | lucide: scissors |
| 18 | `ea-skill-byted-mediakit-audio` | Audio processing, covering audio processing and enhancement, content understanding, etc. Capabilities include: separate-voice, pr… | 字节跳动 / 火山引擎 ByteDance | lucide: music |
| 19 | `ea-skill-byted-mediakit-editing` | Audio/video editing, covering audio/video concatenation, trimming, compositing, etc. Capabilities include: add-image-to-video, ad… | 字节跳动 / 火山引擎 ByteDance | lucide: slice |
| 20 | `ea-skill-byted-mediakit-image` | Image processing, covering image compression, image enhancement, AI processing, etc. Capabilities include: image-ocr, erase-image… | 字节跳动 / 火山引擎 ByteDance | lucide: image |
| 21 | `ea-skill-byted-mediakit-shared` | 1. mediakit-cli: supports a variety of operations such as audio/video processing, editing, and images, with some capabilities cov… | 字节跳动 / 火山引擎 ByteDance | lucide: package |
| 22 | `ea-skill-byted-mediakit-video` | Video processing, covering video quality enhancement, video understanding, subtitle erasure, etc. Capabilities include: analyze-v… | 字节跳动 / 火山引擎 ByteDance | lucide: videotape |
| 23 | `ea-skill-byted-music-generate` | Generate music using Volcengine Imagination API. Supports vocal songs, instrumental BGM, and lyrics generation. Use when the user… | 字节跳动 / 火山引擎 ByteDance | lucide: disc |
| 24 | `ea-skill-byted-podcast-gen` | Synthesize summaries of topics or web page content into podcast audio. Generates final audio based on Volcengine Doubao voice pod… | 字节跳动 / 火山引擎 ByteDance | lucide: podcast |
| 25 | `ea-skill-byted-seedance-video-generate` | Generate videos using Seedance models. Invoke when user wants to create videos from text prompts, images, or reference materials. | 字节跳动 / 火山引擎 ByteDance | lucide: video |
| 26 | `ea-skill-byted-seedream-image-generate` | Generate high-quality images from text prompts using Volcano Engine Seedream models. Supports multiple artistic styles and aspect… | 字节跳动 / 火山引擎 ByteDance | lucide: camera |
| 27 | `ea-skill-byted-sms-sender` | Volcengine SMS service management tool. Use this capability when you need cloud communication features, including sending SMS, qu… | 字节跳动 / 火山引擎 ByteDance | lucide: message-square |
| 28 | `ea-skill-byted-text-to-speech` | Synthesize text to speech (TTS). Uses Volcengine Doubao Voice Synthesis API, supporting streaming synthesis, multiple timbres, sp… | 字节跳动 / 火山引擎 ByteDance | lucide: speech |
| 29 | `ea-skill-byted-voice-to-text` | Speech to text (ASR). Uses Volcengine BigModel ASR for speech recognition, including Flash mode (≤2h/100MB synchronous fast retur… | 字节跳动 / 火山引擎 ByteDance | lucide: file-audio |
| 30 | `ea-skill-byted-web-search` | Volcengine web search API, returns web/image results. Prioritize using this skill for web search scenarios. Trigger words include… | 字节跳动 / 火山引擎 ByteDance | lucide: search |
| 31 | `ea-skill-canvas-design` | Create beautiful visual art in .png and .pdf documents using design philosophy. You should use this skill when the user asks to c… | — | lucide: pen-tool |
| 32 | `ea-skill-changelog-video` | Turn a weekly changelog .md into a finished branded changelog video (square 1080, ~45-60s, Annie VO, animated brand background, m… | Changelog 工作流 | lucide: history |
| 33 | `ea-skill-chargeback-assistant` | Use this skill whenever a user wants to dispute a charge, file a chargeback, contest a transaction, or fight an unauthorized paym… | — | lucide: rotate-ccw |
| 34 | `ea-skill-chart-visualization` | This skill should be used when the user wants to visualize data. It intelligently selects the most suitable chart type from 26 av… | — | lucide: line-chart |
| 35 | `ea-skill-claude-api` | - Reference for the Claude API / Anthropic SDK — model ids, pricing, params, streaming, tool use, MCP, agents, caching, token cou… | — | ✔ 真 logo |
| 36 | `ea-skill-clawsignal` | Real-time AI agent message alerts and coordination platform | — | lucide: signal |
| 37 | `ea-skill-cloud` | Choose, organize, sync, share, and back up personal files across iCloud, Google Drive, Dropbox, and OneDrive. | — | lucide: cloud |
| 38 | `ea-skill-code-documentation` | Use this skill when the user requests to generate, create, or improve documentation for code, APIs, libraries, repositories, or s… | — | lucide: file-code |
| 39 | `ea-skill-code-review` | Expert code audit - correctness, security, performance, maintainability, and codebase fit analysis | — | lucide: git-pull-request |
| 40 | `ea-skill-composition-patterns` | React composition patterns that scale. Use when refactoring components with boolean prop proliferation, building flexible compone… | — | lucide: layout-grid |
| 41 | `ea-skill-consulting-analysis` | Use this skill when the user requests to generate, create, or write professional research reports including but not limited to ma… | — | lucide: briefcase |
| 42 | `ea-skill-crypto` | Cryptocurrency market data and price alert monitoring tool based on CCXT. Supports multiple exchanges, real-time price tracking, … | Bitcoin / 加密资产 | ✔ 真 logo |
| 43 | `ea-skill-cut-the-curve` | The technique catalog: five velocity-matched SEAMS (zoom-through, INVERSE zoom-through, cut-the-curve, waterfall cut, rack-focus … | — | lucide: trending-up |
| 44 | `ea-skill-data-analysis` | Use this skill when the user uploads Excel (.xlsx/.xls) or CSV files and wants to perform data analysis, generate statistics, cre… | — | lucide: pie-chart |
| 45 | `ea-skill-data-files` | Structured data file processing - read, write, convert, and transform CSV, JSON, XML, YAML, TSV; validate and reshape structured … | — | lucide: folder-open |
| 46 | `ea-skill-debug` | Systematic debugging workflow - gather logs, analyze root cause, isolate, fix, and verify errors | — | lucide: bug |
| 47 | `ea-skill-deep-research` | Use this skill instead of WebSearch for ANY question requiring web research. Trigger on queries like "what is X", "explain X", "c… | — | lucide: telescope |
| 48 | `ea-skill-defuddle` | Extract clean markdown content from web pages using Defuddle CLI, removing clutter and navigation to save tokens. Use instead of … | Defuddle | lucide: filter |
| 49 | `ea-skill-deploy-to-vercel` | Deploy applications and websites to Vercel. Use when the user requests deployment actions like "deploy my app", "deploy and give … | Vercel | ✔ 真 logo |
| 50 | `ea-skill-discord-voice` | Real-time voice conversations in Discord voice channels with Claude AI | — | ✔ 真 logo |
| 51 | `ea-skill-doc-coauthoring` | Guide users through a structured workflow for co-authoring documentation. Use when user wants to write documentation, proposals, … | — | lucide: users |
| 52 | `ea-skill-docker` | Docker containers, images, Compose stacks, networking, volumes, debugging, production hardening, and the commands that keep real … | — | ✔ 真 logo |
| 53 | `ea-skill-docx` | Word document (.docx) processing - read, extract text, create, edit (add text/tables), convert, and inspect .docx files | — | lucide: file-type |
| 54 | `ea-skill-douyin-interact-creation` | Create interactive H5 content for Douyin platform that meets submission standards and runs correctly in-app. | 抖音（字节跳动） | ✔ 真 logo |
| 55 | `ea-skill-douyin-interactive-content-publish` | One-click publish tool for Interactive Space. Upload zip+icon to create/update interactive space apps with auto-generated name/de… | 抖音（字节跳动） | lucide: megaphone |
| 56 | `ea-skill-douyinpay-payment-integration` | End-to-end DouyinPay payment integration assistant. Provides product selection, preparation guidance, server SDK integration (Go/… | 抖音支付（字节跳动） | lucide: credit-card |
| 57 | `ea-skill-email-skill` | Email management and automation. Send, read, search, and organize emails across multiple providers. | — | lucide: mail-check |
| 58 | `ea-skill-embedded-captions` | Add captions or subtitles to an existing single-subject talking-head video without editing the footage. Use for plain verbatim ca… | — | lucide: captions |
| 59 | `ea-skill-exa-web-search-free` | Free AI search via Exa MCP. Web search for news/info, code search for docs/examples from GitHub/StackOverflow, company research f… | Exa | lucide: search |
| 60 | `ea-skill-ezbookkeeping` | Use ezBookkeeping API Tools script to record new transactions, query transactions, retrieve account information, retrieve categor… | ezBookkeeping | lucide: calculator |
| 61 | `ea-skill-faceless-explainer` | Turn arbitrary text — an article, notes, a topic, a brief — into a faceless explainer video: there is no site or footage to captu… | — | lucide: monitor-play |
| 62 | `ea-skill-feishu` | Feishu deep integration skill. Not a simple message bridge, but your digital command center. Designed for the high-pressure colla… | 飞书 Lark（字节跳动） | ✔ 真 logo |
| 63 | `ea-skill-figma` | Import Figma content into a HyperFrames composition — rendered assets, brand tokens, components, storyboard sections → reconstruc… | — | ✔ 真 logo |
| 64 | `ea-skill-find-skills` | Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a ski… | — | lucide: compass |
| 65 | `ea-skill-flyai` | Search flights, hotels, attractions, concerts, and travel deals with natural language. | — | lucide: plane |
| 66 | `ea-skill-frontend-design` | Create distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implements working code with ex… | — | lucide: layout |
| 67 | `ea-skill-gen-test` | Test generation - unit tests, edge cases, error paths, and integration tests following project conventions | — | lucide: beaker |
| 68 | `ea-skill-general-video` | Author or edit a custom HyperFrames composition when no specialized workflow fits, or when BRIEF.md sets flow: companion. Use for… | — | lucide: circle-play |
| 69 | `ea-skill-gh-cli` | GitHub CLI (gh) comprehensive reference guide - repositories, issues, pull requests, Actions, projects, releases and more command… | GitHub（Microsoft） | ✔ 真 logo |
| 70 | `ea-skill-git-commit` | Execute standardized Git commits with intelligent staging, automatic Conventional Commits message generation, and custom override… | — | ✔ 真 logo |
| 71 | `ea-skill-gmail-2` | Gmail email access via MorphixAI proxy | — | ✔ 真 logo |
| 72 | `ea-skill-gmail-cleaner` | Clean and organize Gmail accounts in bulk. | — | ✔ 真 logo |
| 73 | `ea-skill-google-docs` | Google Docs API integration with managed OAuth. Create documents, insert text, apply formatting, and manage content. | — | ✔ 真 logo |
| 74 | `ea-skill-goplaces` | Query Google Places API (New) via the goplaces CLI for text search, place details, resolve, and reviews. | Google Places | lucide: map-pinned |
| 75 | `ea-skill-gpt-image-1-5` | Generate and edit images using OpenAI's GPT Image 1.5 model. Use when the user asks to generate, create, edit, modify, change, al… | OpenAI | lucide: wand-sparkles |
| 76 | `ea-skill-gsap-core` | Official GSAP skill for the core API — gsap.to(), from(), fromTo(), easing, duration, stagger, defaults, gsap.matchMedia() (respo… | — | ✔ 真 logo |
| 77 | `ea-skill-gsap-frameworks` | Official GSAP skill for Vue, Svelte, and other non-React frameworks — lifecycle, scoping selectors, cleanup on unmount. Use when … | — | ✔ 真 logo |
| 78 | `ea-skill-gsap-performance` | Official GSAP skill for performance — prefer transforms, avoid layout thrashing, will-change, batching. Use when optimizing GSAP … | — | ✔ 真 logo |
| 79 | `ea-skill-gsap-plugins` | Official GSAP skill for GSAP plugins — registration, ScrollToPlugin, ScrollSmoother, Flip, Draggable, Inertia, Observer, SplitTex… | — | ✔ 真 logo |
| 80 | `ea-skill-gsap-react` | Official GSAP skill for React — useGSAP hook, refs, gsap.context(), cleanup. Use when the user wants animation in React or Next.j… | — | ✔ 真 logo |
| 81 | `ea-skill-gsap-scrolltrigger` | Official GSAP skill for ScrollTrigger — scroll-linked animations, pinning, scrub, triggers. Use when building or recommending scr… | — | ✔ 真 logo |
| 82 | `ea-skill-gsap-timeline` | Official GSAP skill for timelines — gsap.timeline(), position parameter, nesting, playback. Use when sequencing animations, chore… | — | ✔ 真 logo |
| 83 | `ea-skill-gsap-utils` | Official GSAP skill for gsap.utils — clamp, mapRange, normalize, interpolate, random, snap, toArray, wrap, pipe. Use when the use… | — | ✔ 真 logo |
| 84 | `ea-skill-hook-analyzer-skill` | Video first-three-seconds hook analysis skill. Extracts first-three-seconds segment data from breakdown results to provide struct… | — | lucide: webhook |
| 85 | `ea-skill-hyperframes` | Mandatory entry point: read this first for any request to make, create, edit, animate, or render a video, animation, or motion gr… | HyperFrames（自研） | lucide: frame |
| 86 | `ea-skill-hyperframes-animation` | All animation knowledge for HyperFrames — atomic motion rules, multi-phase scene blueprints, scene transitions, broader motion-de… | HyperFrames（自研） | lucide: zap |
| 87 | `ea-skill-hyperframes-cli` | Use the HyperFrames CLI development loop: init, add, catalog, capture, lint, check, snapshot, compare, grade-compare, preview, pl… | HyperFrames（自研） | lucide: square-terminal |
| 88 | `ea-skill-hyperframes-core` | The HyperFrames composition contract — build one renderable project. Use for composition structure, the `data-*` timing attribute… | HyperFrames（自研） | lucide: box |
| 89 | `ea-skill-hyperframes-creative` | Non-animation creative direction for HyperFrames videos. Use for design spec (frame.md / design.md) handling, palettes, typograph… | HyperFrames（自研） | lucide: palette |
| 90 | `ea-skill-hyperframes-keyframes` | Use when a HyperFrames composition needs seek-safe 2D/3D keyframes, GSAP timelines, CSS keyframes, Anime.js, WAAPI, FLIP, paths, … | HyperFrames（自研） | lucide: diamond |
| 91 | `ea-skill-hyperframes-registry` | Install, discover, and wire registry blocks and components into HyperFrames compositions. Use when running hyperframes add or hyp… | HyperFrames（自研） | lucide: library |
| 92 | `ea-skill-iga-pages` | One-click deployment for frontend, full-stack, and AI Agent apps with online preview links, powered by Volcengine IGA. | — | lucide: layers |
| 93 | `ea-skill-image-generation` | Use this skill when the user requests to generate, create, imagine, or visualize images including characters, scenes, products, o… | — | lucide: images |
| 94 | `ea-skill-images` | Image file processing - inspect metadata, OCR text in images, convert formats, and visually understand image content | — | lucide: gallery-vertical |
| 95 | `ea-skill-imap-email` | Read and manage email via IMAP (ProtonMail Bridge, Gmail, etc.). | — | lucide: mail-open |
| 96 | `ea-skill-internal-comms` | A set of resources to help me write all kinds of internal communications, using the formats that my company likes to use. Claude … | — | lucide: radio-tower |
| 97 | `ea-skill-iris-development` | Iris is Redis's umbrella for AI-focused products. Use this skill when integrating with the Iris Redis Agent Memory (RAM) data pla… | — | ✔ 真 logo |
| 98 | `ea-skill-json-canvas` | Create and edit JSON Canvas files (.canvas) with nodes, edges, groups, and connections. Use when working with .canvas files, crea… | — | ✔ 真 logo |
| 99 | `ea-skill-kubectl-skill` | Execute and manage Kubernetes clusters via kubectl commands. Query resources, deploy applications, debug containers, manage confi… | Kubernetes（CNCF） | ✔ 真 logo |
| 100 | `ea-skill-linkedin` | LinkedIn automation via browser relay or cookies for messaging, profile viewing, and network actions. | LinkedIn（Microsoft） | lucide: share |
| 101 | `ea-skill-market-research` | Research markets with sizing, segmentation, competitor mapping, pricing checks, and demand validation that turn fuzzy ideas into … | — | lucide: store |
| 102 | `ea-skill-mcp-builder` | Build high-quality MCP (Model Context Protocol) servers connecting LLMs with external APIs and services, with comprehensive workf… | Model Context Protocol（Anthropic） | lucide: wrench |
| 103 | `ea-skill-media-use` | Agent Media OS, the single skill for every media need in a HyperFrames project. Resolve BGM, SFX, image, icon, brand logo, voice,… | — | lucide: play |
| 104 | `ea-skill-mgrep-code-search` | Semantic code search using mgrep for efficient codebase exploration. This skill should be used when searching or exploring codeba… | — | lucide: file-search |
| 105 | `ea-skill-mongodb` | Design MongoDB schemas with proper embedding, indexing, aggregation, and production-ready patterns. | — | ✔ 真 logo |
| 106 | `ea-skill-motion-doctrine` | GATEWAY — load FIRST before composing any HyperFrames animation or video. The high-level motion law that makes a multi-scene vide… | — | lucide: move |
| 107 | `ea-skill-motion-graphics` | A short, design-led motion graphic where motion is the message — kinetic typography, stat count-up, chart/data-viz hit, logo stin… | — | lucide: orbit |
| 108 | `ea-skill-music-to-video` | Turn a music track (an audio file, a video to pull audio from, or a track generated from a mood brief) into a beat-synced video —… | — | lucide: disc |
| 109 | `ea-skill-n8n-automation` | Manage n8n workflows from OpenClaw via the n8n REST API. Use when the user asks about n8n workflows, automations, executions, or … | — | ✔ 真 logo |
| 110 | `ea-skill-newsletter-generation` | Use this skill when the user requests to generate, create, write, or draft a newsletter, email digest, weekly roundup, industry b… | — | lucide: newspaper |
| 111 | `ea-skill-notion-cli` | - Use the Notion CLI (`ntn`) to interact with the Notion API, manage workers, and upload files. Use when the user asks to "call t… | — | ✔ 真 logo |
| 112 | `ea-skill-notion-integration` | Notion integration - pages, databases, and block management | — | ✔ 真 logo |
| 113 | `ea-skill-obsidian-bases` | Create and edit Obsidian Bases (.base files) with views, filters, formulas, and summaries. Use when working with .base files, cre… | — | ✔ 真 logo |
| 114 | `ea-skill-obsidian-cli` | Interact with Obsidian vaults using the Obsidian CLI to read, create, search, and manage notes, tasks, properties, and more. Also… | — | ✔ 真 logo |
| 115 | `ea-skill-obsidian-markdown` | Create and edit Obsidian Flavored Markdown with wikilinks, embeds, callouts, properties, and other Obsidian-specific syntax. Use … | — | ✔ 真 logo |
| 116 | `ea-skill-ordercli` | Foodora-only CLI for checking past orders and active order status (Deliveroo WIP). | — | lucide: clipboard-list |
| 117 | `ea-skill-outlook-delegate` | Read, search, and manage Outlook emails and calendar via Microsoft Graph API with delegate support. | Microsoft Outlook | lucide: mail-plus |
| 118 | `ea-skill-oversized-cursor` | House-style oversized macOS cursor technique for HyperFrames launch videos. Load whenever a scene involves cursors or a pointer-l… | — | lucide: mouse-pointer-click |
| 119 | `ea-skill-pdf` | PDF document processing - extract text, inspect metadata, render pages for visual understanding, handle large and scanned PDFs | — | lucide: file-type |
| 120 | `ea-skill-porteden-email` | Email Management - Secure Gmail, Outlook & Exchange - multi account support. | — | lucide: at-sign |
| 121 | `ea-skill-pptx` | PowerPoint (.pptx) processing - read, extract text, inspect slides, create presentations, add slides, list slide structure | — | lucide: presentation |
| 122 | `ea-skill-pr-to-video` | Turn a GitHub pull request (a PR URL, owner/repo#N, or 'this PR' in a checked-out repo) into a code-change explainer video — chan… | GitHub PR（Microsoft） | lucide: git-merge |
| 123 | `ea-skill-product-launch-video` | Turn a product or marketing URL, pasted script, or brief into a product launch / promo video — SaaS promos, feature reveals, prod… | 产品发布工作流 | lucide: party-popper |
| 124 | `ea-skill-pubmed-edirect` | Search and retrieve literature from PubMed using NCBI's EDirect command-line tools. ⚠️ Advanced skill requiring manual installati… | — | ✔ 真 logo |
| 125 | `ea-skill-pytest-forge` | - Unit test auto-generation + coverage blind-spot analysis Skill (iFLYTEK AI Data Intelligence Analysis and Application Skill Dev… | — | ✔ 真 logo |
| 126 | `ea-skill-react-best-practices` | React and Next.js performance optimization guidelines from Vercel Engineering. This skill should be used when writing, reviewing,… | — | ✔ 真 logo |
| 127 | `ea-skill-react-native-skills` | React Native and Expo best practices for building performant mobile apps. Use when building React Native components, optimizing l… | — | ✔ 真 logo |
| 128 | `ea-skill-react-view-transitions` | Guide for implementing smooth, native-feeling animations using React's View Transition API (`<ViewTransition>` component, `addTra… | — | ✔ 真 logo |
| 129 | `ea-skill-redbookskills` | Xiaohongshu (RED) auto-publish, comment, and content search | 小红书 | ✔ 真 logo |
| 130 | `ea-skill-redis-clustering` | Redis Cluster and replication guidance covering hash tags for multi-key operations, avoiding CROSSSLOT errors, and reading from r… | — | ✔ 真 logo |
| 131 | `ea-skill-redis-connections` | Redis client and connection guidance covering connection pooling, multiplexing, pipelining, client-side caching with RESP3, avoid… | — | ✔ 真 logo |
| 132 | `ea-skill-redis-core` | Core Redis modeling guidance — choose the right data structure (String, Hash, List, Set, Sorted Set, JSON, Stream, Vector Set) an… | — | ✔ 真 logo |
| 133 | `ea-skill-redis-observability` | Redis observability guidance — which metrics to monitor (memory, connections, hit ratio, ops/sec, rejected connections), which bu… | — | ✔ 真 logo |
| 134 | `ea-skill-redis-search` | Redis Search guidance covering FT.CREATE schema design, field type selection (TEXT, TAG, NUMERIC, GEO, GEOSHAPE, VECTOR, JSON pat… | — | ✔ 真 logo |
| 135 | `ea-skill-redis-security` | Redis security guidance covering authentication (requirepass and ACL users), TLS, ACL-based least-privilege access control, restr… | — | ✔ 真 logo |
| 136 | `ea-skill-redis-semantic-cache` | Redis LangCache guidance for semantic caching of LLM responses on Redis Cloud — calling search/set via the SDK or REST API, tunin… | — | ✔ 真 logo |
| 137 | `ea-skill-refactor` | Behavior-preserving code transformation - extract, rename, decouple, and restructure with zero regression | — | lucide: refresh-cw |
| 138 | `ea-skill-remotion-to-hyperframes` | Port an existing Remotion (React) composition''s source to HyperFrames HTML. Use ONLY on an explicit ask to port/convert/migrate/… | Remotion + HyperFrames | lucide: repeat |
| 139 | `ea-skill-report-generator-skill` | Video analysis report generation skill. Integrates breakdown data and hook analysis results to generate a professional Markdown v… | — | lucide: file-output |
| 140 | `ea-skill-revolut` | Revolut neobank operations - transfers, cards, currency exchange | — | ✔ 真 logo |
| 141 | `ea-skill-s3` | Work with S3-compatible object storage with proper security, lifecycle policies, and access patterns. | AWS S3（Amazon） | lucide: hard-drive |
| 142 | `ea-skill-seam-craft` | Render-correctness doctrine for scene-to-scene seams in HyperFrames launch videos — the prerequisites that make transitions compo… | — | lucide: scissors-line-dashed |
| 143 | `ea-skill-security-best-practices` | Perform language and framework specific security best-practice reviews and suggest improvements. Trigger only when the user expli… | — | lucide: shield-check |
| 144 | `ea-skill-skill-creator` | Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a skill from … | — | lucide: package-plus |
| 145 | `ea-skill-slack` | Use when you need to control Slack from Clawdbot via the slack tool, including reacting to messages or pinning/unpinning items in… | Slack（Salesforce） | ✔ 真 logo |
| 146 | `ea-skill-slack-gif-creator` | Knowledge and utilities for creating animated GIFs optimized for Slack. Provides constraints, validation tools, and animation con… | Slack（Salesforce） | lucide: image-play |
| 147 | `ea-skill-slack-integration` | Slack integration - messaging, channel management, conversation search | Slack（Salesforce） | lucide: plug-zap |
| 148 | `ea-skill-slideshow` | Author a HyperFrames slideshow — a presentation, pitch deck, or interactive deck with discrete slides, fragment reveals, branchin… | — | lucide: gallery-horizontal |
| 149 | `ea-skill-spotify` | Control Spotify playback on macOS. Play/pause, skip tracks, control volume, play artists/albums/playlists. Use when a user asks t… | — | ✔ 真 logo |
| 150 | `ea-skill-stripemeter` | Integrate Stripe usage-based billing with idempotent event ingestion, late-event handling, and pre-invoice reconciliation. | Stripe | ✔ 真 logo |
| 151 | `ea-skill-stuck` | Self-diagnosis for stuck or looping agents - detect patterns, identify root cause, and apply recovery strategies | — | lucide: life-buoy |
| 152 | `ea-skill-systematic-literature-review` | Use this skill when the user wants a systematic literature review, survey, or synthesis across multiple academic papers on a topi… | — | lucide: book-marked |
| 153 | `ea-skill-talking-head-recut` | Package an existing talking-head / interview / podcast video with timed, designed GRAPHIC OVERLAY cards — kinetic titles, lower-t… | — | lucide: square-user |
| 154 | `ea-skill-theme-factory` | Toolkit for styling artifacts with a theme. These artifacts can be slides, docs, reportings, HTML landing pages, etc. There are 1… | — | lucide: paintbrush |
| 155 | `ea-skill-todoist-api` | This skill provides instructions for interacting with Todoist using the td CLI tool. It covers CRUD operations for tasks/projects… | — | ✔ 真 logo |
| 156 | `ea-skill-travel-destination` | Destination discovery guidance - find attractions, POIs, neighborhoods, food, and what to do at a destination (TripAdvisor, Googl… | — | lucide: map |
| 157 | `ea-skill-travel-flights` | Flight search and booking guidance - find flights, compare fares, track prices, understand routes and layovers via aggregator sit… | — | lucide: plane-takeoff |
| 158 | `ea-skill-travel-hotels` | Hotel and lodging search guidance - compare prices, read reviews, find short-stay rentals via aggregators (Booking, Agoda, Airbnb… | — | lucide: bed |
| 159 | `ea-skill-travel-itinerary` | Trip planning guidance - build multi-day itineraries, estimate logistics and pacing, combine transport + stays + activities (Rome… | — | lucide: route |
| 160 | `ea-skill-travel-trains` | Train and rail search guidance - find train tickets, schedules, fares, passes (China 12306, Europe Eurail/Trainline, Japan JR) vi… | — | lucide: train-front |
| 161 | `ea-skill-travel-transit` | Local transit and navigation guidance - city metro, buses, taxi/ride-hail, walking directions, real-time traffic (Google Maps, Ci… | — | lucide: bus-front |
| 162 | `ea-skill-travel-visa` | Visa and entry requirements guidance - check visa policy, required documents, e-visa eligibility, passport validity (iVisa, Sherp… | — | lucide: stamp |
| 163 | `ea-skill-travel-weather` | Travel weather guidance - destination forecast, best season to visit, what to pack, severe-weather awareness (OpenWeatherMap, Acc… | — | lucide: cloud-sun |
| 164 | `ea-skill-upstash-redis-kv` | Read and write to Upstash Redis-compatible key-value store via REST API. Use when there is a need to save or retrieve key-value d… | — | ✔ 真 logo |
| 165 | `ea-skill-vercel-cli-with-tokens` | Deploy and manage projects on Vercel using token-based authentication. Use when working with Vercel CLI using access tokens rathe… | — | ✔ 真 logo |
| 166 | `ea-skill-vercel-optimize` | Use for Vercel cost and performance optimization on deployed projects, especially Next.js, SvelteKit, Nuxt, and limited Astro app… | — | ✔ 真 logo |
| 167 | `ea-skill-verify` | Code verification pipeline - static analysis, type checking, linting, test execution, build check, smoke test | — | lucide: badge-check |
| 168 | `ea-skill-video` | Video file processing - inspect media info, convert formats, extract audio, extract frames, compress, trim, screenshot a frame, t… | — | lucide: clapperboard |
| 169 | `ea-skill-video-breakdown-skill` | Video breakdown skill (self-contained, no external backend needed). Uses FFmpeg to preprocess video and generate segment data. (1… | — | lucide: list-video |
| 170 | `ea-skill-video-generation` | Use this skill when the user requests to generate, create, or imagine videos. Supports structured prompts and reference image for… | — | lucide: square-play |
| 171 | `ea-skill-vue` | Build Vue 3 applications with Composition API, proper reactivity patterns, and production-ready components. | — | lucide: component |
| 172 | `ea-skill-web-artifacts-builder` | Suite of tools for creating elaborate, multi-component claude.ai HTML artifacts using modern frontend web technologies (React, Ta… | — | lucide: hammer |
| 173 | `ea-skill-web-design-guidelines` | Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit design", … | — | lucide: pencil-ruler |
| 174 | `ea-skill-webapp-testing` | Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debu… | — | lucide: monitor-check |
| 175 | `ea-skill-wechat-auto-reply` | Semi-automatic reply to WeChat contact messages (auto-send when confidence >85%, otherwise confirm before sending), or proactivel… | — | ✔ 真 logo |
| 176 | `ea-skill-wechat-toolkit` | WeChat public account all-in-one toolkit - integrates article search, article download, AI rewriting, and public account publishi… | — | ✔ 真 logo |
| 177 | `ea-skill-wecom` | WeCom (Enterprise WeChat) webhook message sending | 企业微信（腾讯） | ✔ 真 logo |
| 178 | `ea-skill-weixin-connect` | Connect personal WeChat (not Enterprise WeChat). Used when user says \"connect personal WeChat\", \"integrate personal WeChat\", … | 微信（腾讯） | ✔ 真 logo |
| 179 | `ea-skill-whatsapp-styling-guide` | Skill to ensure all messages sent to WhatsApp follow the platform's specific formatting syntax. | — | ✔ 真 logo |
| 180 | `ea-skill-write-docs` | Technical documentation writer - API reference, README, ADR, changelog, and tutorials with quality rigor | — | lucide: pencil |
| 181 | `ea-skill-writing-guidelines` | Review docs/prose for Writing Guidelines compliance. Use when asked to "review my docs", "check writing style", "audit prose", "r… | — | lucide: pen-line |
| 182 | `ea-skill-xlsx` | Excel/spreadsheet (.xlsx/.xls) processing - read cells, write data, list sheets, handle formulas and tabular data | — | lucide: sheet |
| 183 | `ea-skill-xquik-x-twitter-scraper` | Safety-reviewed guide for @xquik/tweetclaw, the Xquik Encre plugin for structured X/Twitter workflows. | X（原 Twitter） | ✔ 真 logo |
| 184 | `ea-skill-yelp` | Search Yelp businesses and reviews, compare local options, and audit listing quality with official APIs, public pages, and safe a… | — | ✔ 真 logo |
| 185 | `ea-skill-youtube-transcript` | Extract transcripts from YouTube videos. Use when the user asks for a transcript, subtitles, or captions of a YouTube video and p… | — | ✔ 真 logo |

## 三、全部工具（ea-tool-*）

| # | 插件 | 功能 | 品牌 | 图标 |
|---|---|---|---|---|
| 1 | `ea-tool-agent` | Sub-agent delegation skill. prompt/agent_name/tasks, fan out parallel or specialized work | — | lucide: bot |
| 2 | `ea-tool-apply-patch` | Structured patch skill. patch/root/dry_run, multi-file diffs, when to use vs file_edit/file_write | — | lucide: file-pen |
| 3 | `ea-tool-archive` | Create, extract, list, or inspect archives in zip, tar, gz, bz2, or xz format. Use this instead of bash `tar`/`zip`/`unzip` -- it… | — | lucide: archive |
| 4 | `ea-tool-browser` | Drive the embedded Chromium browser - navigate, click/type by text, role, selector, or coordinates; tabs, screenshots, CDP access… | — | lucide: app-window |
| 5 | `ea-tool-chart` | Generate data visualizations with matplotlib - line, bar, scatter, pie, histogram, area, or heatmap - and return a PNG (base64) o… | — | lucide: chart-column |
| 6 | `ea-tool-cloud-storage` | Cloud object-storage operations for AWS S3, Google Cloud Storage (GCS), and Azure Blob Storage behind one interface. Actions vary… | — | lucide: hard-drive-upload |
| 7 | `ea-tool-computer-use` | WHAT: The single computer-control tool -- one curated action schema routing each action to the browser or desktop backend, return… | — | lucide: mouse-pointer |
| 8 | `ea-tool-cron-create` | Schedule a prompt to run automatically on a recurring cron schedule. Use this for reminders, periodic reports, polling, or any ta… | — | lucide: calendar-plus |
| 9 | `ea-tool-cron-delete` | Cancel or delete a previously scheduled cron job by its ID so it stops firing. Use this when a scheduled task is no longer needed… | — | lucide: calendar-x |
| 10 | `ea-tool-cron-list` | List all currently scheduled cron jobs with their IDs, names, schedule expression, and next fire time. Use this to review active … | — | lucide: calendar-clock |
| 11 | `ea-tool-database` | Database query skill. sql/database_url/limit, run SQL without bare sqlite3/psql | — | lucide: database |
| 12 | `ea-tool-deploy` | Deployment skill. target/action/config_file/project_name, deploy apps without bare kubectl/gcloud/vercel | — | lucide: cloud-upload |
| 13 | `ea-tool-device-battery` | WHAT: Returns the current battery state -- charge percentage, whether AC power is plugged in, and estimated seconds remaining (wh… | — | lucide: battery-charging |
| 14 | `ea-tool-device-info` | WHAT: Returns system hardware information -- CPU model and core count, total RAM, aggregate disk capacity, and the current workin… | — | lucide: smartphone |
| 15 | `ea-tool-device-location` | WHAT: Returns the device's geographic location via the OS-native location service -- timezone, UTC offset, and GPS coordinates (l… | — | lucide: map-pin |
| 16 | `ea-tool-device-network` | WHAT: Reports live network interface information -- hostname, IPv4/IPv6 addresses, MAC addresses, link speed, and per-interface c… | — | lucide: wifi |
| 17 | `ea-tool-device-sensor` | WHAT: Returns live readings from device sensors such as accelerometer, gyroscope, magnetometer, and ambient light when the hardwa… | — | lucide: activity |
| 18 | `ea-tool-diagram` | Render text-based diagrams (Mermaid, Graphviz/DOT, or PlantUML) into PNG/SVG images, or save the DSL source for later rendering. … | — | lucide: git-fork |
| 19 | `ea-tool-diff` | Compare text, files, or directories and return a structured diff. Supports unified/context/JSON/HTML output formats for text and … | — | lucide: file-diff |
| 20 | `ea-tool-docker` | Docker skill. command/image_or_container/options, manage containers/images without bare docker CLI | — | ✔ 真 logo |
| 21 | `ea-tool-document` | Word document skill. action/file_path/content/text/data, read/create/edit .docx without manual python-docx | — | lucide: files |
| 22 | `ea-tool-email` | Send and read emails via SMTP/IMAP -- compose and send messages, read the inbox, search by criteria, or list available folders. W… | — | lucide: mail |
| 23 | `ea-tool-env-manager` | Read and modify environment variables and config files. The 'process' scope mutates the current process environment (get/set/dele… | — | lucide: sliders-horizontal |
| 24 | `ea-tool-generate-image` | Edit an existing image with a text prompt, optionally constrained to the area revealed by a mask. Use this to inpaint part of an … | — | lucide: image-plus |
| 25 | `ea-tool-git-tool` | Git operations skill. command/repo_path/args, when to use git tool vs bash, destructive-op caution | — | ✔ 真 logo |
| 26 | `ea-tool-github` | Interact with the GitHub and GitLab REST APIs to manage issues, pull/merge requests, repositories, releases, gists, and CI Action… | — | ✔ 真 logo |
| 27 | `ea-tool-glob` | File-by-pattern finding skill. pattern/path, find files by name not content, pair with grep/file_read | — | lucide: folder-search |
| 28 | `ea-tool-grep` | Ripgrep content search skill. When to use grep, parameter tips, multiline pitfalls, pairing with glob/lsp | — | lucide: text-search |
| 29 | `ea-tool-hash-crypto` | Compute hashes (MD5/SHA1/SHA256/SHA512), HMACs, and file checksums, perform AES symmetric encrypt/decrypt (CBC/ECB/CTR/GCM), and … | — | lucide: key-round |
| 30 | `ea-tool-image` | Image processing skill. action/file_path/options, analyze/convert images without bare imagemagick | — | lucide: image |
| 31 | `ea-tool-info` | Info card skill. display/content/title/type, render a self-contained HTML/CSS/JS card in chat | — | lucide: info |
| 32 | `ea-tool-json-tool` | Validate, format, minify, transform, infer a schema from, or query JSON documents. Accepts input either as an inline 'data' strin… | — | ✔ 真 logo |
| 33 | `ea-tool-lint-format` | Run a linter and/or formatter on the workspace and return a structured JSON report. Supports ruff (Python), eslint + prettier (Ja… | — | lucide: brush |
| 34 | `ea-tool-lsp` | Language server skill. goToDefinition/findReferences/hover/documentSymbol, semantic navigation vs grep | — | lucide: code |
| 35 | `ea-tool-media` | Media skill. action/file_path/output_path/codec/bitrate/format/quality/fps, process video/audio via ffmpeg without manual cli | — | lucide: film |
| 36 | `ea-tool-media-api` | Generate vector embeddings for text inputs using the active backend's embeddings model. Use this to power semantic search, cluste… | — | lucide: square-play |
| 37 | `ea-tool-notebook` | Jupyter notebook skill. action/code/cell_id/cell_type/kernel_name, execute and edit notebooks | — | lucide: notebook-pen |
| 38 | `ea-tool-notify` | Send a notification to the user or an external channel via webhook, Slack, Discord, or a native desktop toast. | — | lucide: bell |
| 39 | `ea-tool-pdf` | PDF skill. action/file_path/pages, read/extract/merge PDFs without bare pdftotext | — | lucide: file-text |
| 40 | `ea-tool-platform-api` | WHAT: Create, retrieve, or list asynchronous batch processing jobs against the OpenAI-compatible Batch API. Batches run large vol… | — | lucide: plug |
| 41 | `ea-tool-presentation` | PowerPoint skill. action/file_path/title/slides/slide_type/content, read/create/edit .pptx without manual python-pptx | — | lucide: gallery-vertical-end |
| 42 | `ea-tool-qr-code` | Generate QR codes as PNG/SVG images, or decode QR codes and barcodes from an existing image. Use `generate` for raster PNG/JPG/BM… | — | lucide: qr-code |
| 43 | `ea-tool-question` | User clarification skill. question/details/options/questions, ask the user when genuinely blocked | — | lucide: help-circle |
| 44 | `ea-tool-rest-client` | Make an HTTP request to a REST or GraphQL API endpoint and return the status code, headers, and parsed body. | — | lucide: arrow-left-right |
| 45 | `ea-tool-skill` | Skill activation tool. name/args, activate a domain skill (travel-flights, pdf, data-viz) by name to inject its guidance | — | lucide: sparkles |
| 46 | `ea-tool-spreadsheet` | Spreadsheet skill. action/file_path/sheet_name/data/range, read/write Excel/CSV without awk | — | lucide: table |
| 47 | `ea-tool-ssh` | Run commands, transfer files, or execute multi-line scripts on a remote host via SSH/SCP using the system ssh and scp clients. Us… | OpenSSH | lucide: terminal |
| 48 | `ea-tool-task-create` | Create a new background sub-task (bash command, delegated agent, or workflow) that runs independently and can be polled later. | — | lucide: list-plus |
| 49 | `ea-tool-task-get` | Get the details (name, type, status, description, result, error, parent) of a single task by its ID. | — | lucide: list-checks |
| 50 | `ea-tool-task-list` | List all tasks with optional status filter | — | lucide: list |
| 51 | `ea-tool-task-output` | Retrieve output from a running or completed background task. Can block waiting for completion, or return current status. | — | lucide: scroll-text |
| 52 | `ea-tool-task-stop` | Stop a running background task by marking it cancelled. | — | lucide: square |
| 53 | `ea-tool-task-update` | Update the status and/or result/error of an existing task. | — | lucide: list-todo |
| 54 | `ea-tool-test-runner` | Run the project's test suite and return a structured JSON report. Auto-detects pytest, vitest, jest, and cargo test from project … | — | lucide: flask-conical |
| 55 | `ea-tool-todo` | Session task list skill. Replacement semantics, stable ids, exactly-one-in_progress discipline, when a list earns its keep | — | lucide: check-square |
| 56 | `ea-tool-translation` | Translate text between languages, detect the source language, or list supported languages; the default `service='auto'` chains Go… | — | lucide: languages |
| 57 | `ea-tool-web-fetch` | Fetch a single URL and return its content as text, markdown, or raw HTML. Exa crawler (JS rendering) first, SSRF-guarded httpx fa… | — | lucide: globe |
| 58 | `ea-tool-workflow` | Multi-agent workflow skill. goal + mode (light pipeline or team with review gate and consensus), timeout_seconds, DAG orchestrati… | — | lucide: git-branch |
