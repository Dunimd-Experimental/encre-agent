
## Video Embedding (Important)

WeChat videos must use iframe + data-mpvid format, `publish_with_video.js` already has this logic built-in.

Reference in Markdown:
```markdown
![Video description](media/video.mp4)   # Auto upload and embed
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| IP not in whitelist | `rest_client ifconfig.me` → Add to public account backend |
| Built-in wenyan not ready | `node {baseDir}/scripts/bootstrap/install_wenyan.js` |
| Environment variables not set | `export WECHAT_APP_ID=xxx` |
| Missing frontmatter | Add title + cover |
| 40001 token expired | Use `publish_with_video.js` (has built-in token management) |
| Image paths with spaces | Rename directory/file, ensure cover and body image paths have no spaces |

---

# Complete Workflow Examples

## Search → Rewrite → Publish

```
1. Search articles: node {baseDir}/scripts/search/search_wechat.js "AI tutorial" -n 5 -c
2. Select target article, execute rewriting
3. Save as Markdown (with frontmatter)
4. Publish: node {baseDir}/scripts/publisher/publish.js article.md
```

## Download → Rewrite → Publish

```
1. Download article: node {baseDir}/scripts/downloader/download.js "https://mp.weixin.qq.com/s/xxx"
2. Read downloaded HTML/Markdown, execute rewriting
3. Save as Markdown (with frontmatter)
4. Publish: node {baseDir}/scripts/publisher/publish.js article.md
```

---

## Important Notes

- All tools are for personal learning use only, please comply with copyright laws
- Search function has built-in anti-ban mechanisms (random UA, request delay), do not use at high frequency
- Config file: downloader `{baseDir}/scripts/downloader/config.json`
