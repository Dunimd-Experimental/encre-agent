---
name: wechat-toolkit
description: WeChat public account all-in-one toolkit - integrates article search, article download, AI rewriting, and public account publishing. Used when user needs to search/download/rewrite/publish WeChat public account articles.
hidden: true
context: inline
---

## Wechat Toolkit
# WeChat Public Account Toolkit (wechat-toolkit)

Integrates four major functional modules: **Search → Download → Rewrite → Publish**, covering the entire public account content creation workflow.

---

## Module Overview

| Module | Function | Trigger Example |
|--------|----------|-----------------|
| 🔍 Search | Search public account articles by keyword | "Search XX's public account articles" |
| 📰 Download | Download article content/images/videos | "Download this public account article" |
| ✍️ Rewrite | AI de-trace + original rewriting | "Help me rewrite this article" |
| 📱 Publish | Publish Markdown to draft box | "Publish to public account" |

---

# 🔍 Module 1: Article Search

Search public account articles via Sogou WeChat search, supports fetching full text.

## First-time Dependency Installation

```shell
npm install -g cheerio
```

## Usage

```shell
# Basic search
node {baseDir}/scripts/search/search_wechat.js "keyword"

# Specify count
node {baseDir}/scripts/search/search_wechat.js "keyword" -n 15

# Save to file
node {baseDir}/scripts/search/search_wechat.js "keyword" -n 20 -o result.json

# Resolve real links
node {baseDir}/scripts/search/search_wechat.js "keyword" -n 5 -r

# Fetch article content (auto-enables -r)
node {baseDir}/scripts/search/search_wechat.js "keyword" -n 5 -c
```

### Parameter Description
- `query`: Search keyword (required)
- `-n, --num`: Number of results (default 10, max 50)
- `-o, --output`: Output JSON file path
- `-r, --resolve-url`: Resolve real WeChat article links
- `-c, --fetch-content`: Fetch article body content (auto-enables -r)

### Output Fields
- Article title, URL, summary, publish time, source public account
- `content`: Body content (when using -c)
- `word_count`: Word count (when using -c)

---

# 📰 Module 2: Article Download

Input public account article link, automatically download content (Markdown+HTML), images and videos.

## First-time Dependency Installation

```shell
cd {baseDir}/scripts/downloader && npm install
```

## Before Downloading: Confirm Save Location

```shell
# View current config
node {baseDir}/scripts/downloader/download.js --show-config

# Set default download path (only needed once)
node {baseDir}/scripts/downloader/download.js --set-output ~/Downloads/wechat-articles
```

- `"isDefault": true` → Not configured yet, need to ask user
- `"isDefault": false` → Already configured, inform user of current path

## Download Article

```shell
# Use default path
node {baseDir}/scripts/downloader/download.js "<article URL>"

# Temporarily specify path
node {baseDir}/scripts/downloader/download.js "<article URL>" --output <temp directory>

# Skip images/videos
node {baseDir}/scripts/downloader/download.js "<article URL>" --no-image
node {baseDir}/scripts/downloader/download.js "<article URL>" --no-video
```

### Output Structure
```
<download directory>/<article title>/
├── content/article.html      # Full HTML
├── metadata.json              # Title, author, time, etc.
├── images/                    # All images
└── videos/                    # All videos/audio
```

### Prerequisites
- Node.js ≥ 18, Google Chrome, `terminal npm install` (first time)

---

# ✍️ Module 3: Article Rewriting

Rewrite articles into natural, original style, removing AI writing traces, improving originality.

## Trigger Words
- "Help me rewrite this article"
- "Rewrite into original"
- "Reduce plagiarism rate"
- "Remove AI feel"

## Rewrite Workflow

### Standard Rewrite Process

1. **Get original text** — Obtain original text via search (-c fetch body) or download
2. **Analyze structure** — Identify article type, core arguments, paragraph hierarchy
3. **Deep rewrite** — Execute rewriting according to the following strategies
4. **Add frontmatter** — Add title + cover
5. **Publish** — Push to public account draft box

### Rewrite Strategies

#### A. Structural Restructuring (Core for plagiarism reduction)
- **Paragraph reordering**: Adjust paragraph order, disrupt original structure
- **Paragraph split/merge**: Split long paragraphs into short ones, or merge fragmented paragraphs
- **Narrative perspective shift**: Timeline ↔ Problem-oriented ↔ Comparative analysis ↔ Story introduction
- **Argument restructuring**: Retain core arguments, change presentation method

#### B. Language Rewriting (Remove AI traces)
- **Remove inflated sentences**: "Landmark", "Milestone", "Far-reaching impact" → Replace with specific facts
- **Remove false authority**: "Experts believe", "Industry generally believes" → State source or delete
- **Remove pseudo-depth verbs**: "Enhance capabilities", "Empower", "Drive progress" → Change to specific actions
- **Remove advertising tone**: "Excellent", "Ultimate experience", "Comprehensive" → Objective description
- **Remove AI high-frequency words**: Empower, closed-loop, ecosystem, leverage point, underlying logic, paradigm, sedimentation, potential
- **Remove filler phrases**: In fact, it is worth noting that, generally speaking, it is not hard to find
- **Remove empty conclusions**: "Promising future", "Worth looking forward to" → Actual conclusions or action items

#### C. Title Rewriting
Generate 3-5 alternative titles for each article, covering:
- **Question type**: Use questions to spark curiosity ("Why is XX still using this method?")
- **Number type**: Use numbers to enhance credibility ("3 overlooked XX techniques")
- **Suspense type**: Create information asymmetry ("The truth about XX, 90% of people don't know")
- **Pain point type**: Strike reader pain points ("Stop XX, try this method instead")

#### D. Opening Rewriting
Convert the original opening to one of the following styles:
- **Story introduction**: Start with a small story or scenario
- **Data introduction**: Start with shocking data
- **Pain point introduction**: Hit reader's pain points directly
- **Rhetorical question introduction**: Raise counter-intuitive questions

#### E. SEO Optimization (Optional)
- Naturally embed core keywords in title and first paragraph
- Distribute long-tail keywords in subheadings
- Control keyword density (2%-5%), maintain natural readability

### AI Trace Identification Checklist

After rewriting, check item by item:

| # | Check Item | Action |
|---|------------|--------|
| 1 | Inflated sentences | Replace with specific facts |
| 2 | False authority citations | State source or delete |
| 3 | Pseudo-depth verbs | Change to specific actions |
| 4 | Advertising tone | Objective description |
| 5 | Template paragraphs (challenge→opportunity→outlook) | Delete template, keep conclusion |
| 6 | AI high-frequency words appearing densely | Replace with everyday language |
| 7 | Abusive negative conjunctions (not only... but also...) | Express directly |
| 8 | Forced three-part structure | Keep key points, delete fillers |
| 9 | Mechanical synonym rotation | Use consistent terms for same concept |
| 10 | Dash abuse | Change to periods or commas |
| 11 | Bold emphasis abuse | Remove unnecessary emphasis |
| 12 | List template (**X:**…) | Merge into natural paragraphs |
| 13 | Concept-stacked titles | Change to conversational titles |
| 14 | Emoji overuse | Remove by default unless style specified |
| 15 | Chat language residue | Delete |
| 16 | Knowledge cutoff statement | Delete |
| 17 | Overly ingratiating tone | Respond objectively |
| 18 | Filler phrases | Delete |
| 19 | Excessive vagueness (might, perhaps) | Change to conditional statements |
| 20 | Empty ending | Change to actual conclusion |
| 21 | False range expressions (from…to…) | List specific facts |

### Output Format

1. **Rewritten full text** (Markdown format, with frontmatter)
2. **Alternative titles** (3-5 options)
3. **Change notes** (optional, briefly list major changes)

### Judgment Criteria

Signs of successful rewriting:
- ✅ Reads like a real person wrote it, can be read aloud without awkwardness
- ✅ No empty sentences, template paragraphs, or "AI-like" smell
- ✅ High information density, every sentence has specific content
- ✅ Structure is clearly different from original
- ✅ Preserves core information integrity of original

---

# 📱 Module 4: Article Publishing

One-click publish Markdown to WeChat public account draft box, based on wenyan-cli.

## First-time Installation

```shell
node {baseDir}/scripts/bootstrap/install_wenyan.js
```

Note:
- The skill has built-in forked `wenyan-cli` source code, located at `vendor/wenyan-cli-main`
- The first time you run the publish script, this step will also be executed automatically
- If `pnpm` is not installed locally, first run `corepack enable`

## Configure API Credentials

Ensure environment variables are set (or configured in TOOLS.md):
```shell
export WECHAT_APP_ID=your_wechat_app_id
export WECHAT_APP_SECRET=your_wechat_app_secret
```

**Important:** IP must be in the WeChat public account backend whitelist!

## Markdown Format Requirements

File top **must** contain complete frontmatter:

```markdown
---
title: Article Title (Required!)
cover: https://example.com/cover.jpg  # Cover image (Required!)
---

# Body...
```

⚠️ `title` and `cover` **both required**, otherwise error.

**⚠️ Image paths must use absolute paths** to avoid wenyan path resolution issues. This includes cover and all image references in the body:
```markdown
cover: /Users/username/photos/cover.jpg        # ✅ Absolute path
cover: ./assets/cover.jpg                         # ❌ Relative path may cause errors

![Image](/Users/username/photos/image.jpg)       # ✅ Absolute path
![Image](./images/photo.jpg)                       # ❌ Relative path may cause errors
```

**⚠️ No spaces in image paths.** If `cover` or body images have spaces, wenyan will fail when uploading to public account. It is recommended that article directories, `media/` directories, and all filenames use no-space naming.

## Image Generation

Before publishing, **proactively ask the user if they need images generated**:

> 📸 Article ready! Need me to generate images for you?
> - Cover image (recommended 1080×864)
> - Body illustrations (generated based on paragraph themes)
> - No, just publish

**If the user wants images:**
1. Generate suitable image description prompts based on article title and content
2. Call the user's provided **image generation skill** (such as doubao-image, openai-image-gen, etc.) to generate images
3. Save generated images to the article directory, use **absolute paths** for references
4. Set cover image to the `cover` field in frontmatter
5. Insert body illustrations at appropriate positions using `![description](absolute path)`

**Prompt suggestions:**
- Cover image: Strongly related to title, concise and impactful, suitable for small-size preview
- Body illustrations: Consistent with the corresponding paragraph content, aid understanding

## Publishing Methods

```shell
# Method 1: Use publish.js
node {baseDir}/scripts/publisher/publish.js /path/to/article.md

# Method 2: Use wenyan-cli directly
wenyan publish -f article.md -t lapis -h solarized-light

# Method 3: stdin (recommended, solves path issues)
# macOS/Linux:
cat "/path/to/article.md" | WECHAT_APP_ID=xxx WECHAT_APP_SECRET=xxx wenyan publish -t lapis -h solarized-light

# Method 4: Articles with video (must use this)
node {baseDir}/scripts/publisher/publish_with_video.js /path/to/article.md

# Method 5: Draft / Published article management
node {baseDir}/scripts/publisher/manage_draft.js get MEDIA_ID
node {baseDir}/scripts/publisher/manage_draft.js list --count 10
node {baseDir}/scripts/publisher/manage_draft.js count
node {baseDir}/scripts/publisher/manage_draft.js delete MEDIA_ID
node {baseDir}/scripts/publisher/manage_draft.js publish MEDIA_ID --wait
node {baseDir}/scripts/publisher/manage_draft.js status PUBLISH_ID
node {baseDir}/scripts/publisher/manage_draft.js published-list --count 10
node {baseDir}/scripts/publisher/manage_draft.js published-get ARTICLE_ID
node {baseDir}/scripts/publisher/manage_draft.js published-delete ARTICLE_ID --index 0
```

## Draft Deletion and Formal Publishing

After wenyan extension, besides "upload to draft box", it also supports:

```shell
# Use wenyan directly
wenyan draft get MEDIA_ID
wenyan draft list --count 10
wenyan draft count
wenyan draft delete MEDIA_ID
wenyan draft publish MEDIA_ID --wait
wenyan publish-status PUBLISH_ID
wenyan published list --count 10
wenyan published get ARTICLE_ID
wenyan published delete ARTICLE_ID --index 0

# Or use toolkit wrapper scripts (automatically reads credentials from TOOLS.md)
node {baseDir}/scripts/publisher/manage_draft.js get MEDIA_ID
node {baseDir}/scripts/publisher/manage_draft.js list --count 10
node {baseDir}/scripts/publisher/manage_draft.js count
node {baseDir}/scripts/publisher/manage_draft.js delete MEDIA_ID
node {baseDir}/scripts/publisher/manage_draft.js publish MEDIA_ID --wait
node {baseDir}/scripts/publisher/manage_draft.js status PUBLISH_ID
node {baseDir}/scripts/publisher/manage_draft.js published-list --count 10
node {baseDir}/scripts/publisher/manage_draft.js published-get ARTICLE_ID
node {baseDir}/scripts/publisher/manage_draft.js published-delete ARTICLE_ID --index 0
```

Notes:
- `draft list` / `published list` support `--offset`, `--count`, `--no-content`
- `draft publish` returns an async publish task, recommend using `--wait`
- `publish-status` is used to check formal publish results
- `published delete --index 0` deletes the entire published article; specify a sequence number to delete a single article
- Formal publishing capability depends on public account permissions; if WeChat returns unauthorized, check interface permissions on the public platform

## Theme Options

First, check the theme directory already organized by wechat-toolkit:

```shell
node {baseDir}/scripts/publisher/publish.js --list-themes
```

**Bundled Themes (12)**

- Built-in: `default`, `orangeheart`, `rainbow`, `lapis`, `pie`, `maize`, `purple`, `phycat`
- Custom: `aurora`, `newsroom`, `sage`, `ember`

**Code Highlighting**: `atom-one-dark`, `atom-one-light`, `dracula`, `github`, `github-dark`, `monokai`, `solarized-dark`, `solarized-light`, `xcode`

**Theme Previews (Encre Version Notes)**

- Encre release package **does not include PNG previews** by default, to avoid triggering non-text file restrictions and size limits
- If you want to generate reference images locally, run:

```shell
node {baseDir}/scripts/publisher/publish.js --generate-theme-previews
```

- Output directory: `{baseDir}/scripts/publisher/theme_previews/`

```shell
# Publish with bundled theme
node {baseDir}/scripts/publisher/publish.js article.md lapis
node {baseDir}/scripts/publisher/publish.js article.md aurora

# Specify highlighting theme
node {baseDir}/scripts/publisher/publish.js article.md newsroom github

# Regenerate all reference images
node {baseDir}/scripts/publisher/publish.js --generate-theme-previews
```


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
