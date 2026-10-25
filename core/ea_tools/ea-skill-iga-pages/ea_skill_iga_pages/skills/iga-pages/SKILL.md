---
name: iga-pages
description: One-click deployment for frontend, full-stack, and AI Agent apps with online preview links, powered by Volcengine IGA.
---

# IGA Pages Skill

Deploy frontend, full-stack, and AI Agent apps to the cloud with shareable preview links.

## Critical Prerequisites

**CLI Version**: `@iga-pages/cli` must be >= 1.0.5
```shell
npm i -g @iga-pages/cli@latest
iga --version
```

**Framework Compatibility**: Supported: Next.js, Vite, Vue CLI, CRA, Angular, Hexo, Docusaurus, VitePress, VuePress, Hugo. Also supports plain static HTML/JS/CSS.

Unsupported (inform user before proceeding): Nuxt, Remix, Astro.

## Authentication

### Local IDE (VS Code, TRAE computer, etc.)
```shell
iga login
# Opens browser for OAuth. Wait for success message.
```

### Remote / Headless (SSH, CI/CD, cloud container, etc.)
```shell
iga login --accessKey <YOUR_AK> --secretKey <YOUR_SK>
# Obtain AK/SK from Volcengine IAM console.
```

## Workflow

All commands must run inside the project root.

### New Project (scaffold then deploy)
```shell
npx create-next-app@latest my-app --yes
cd my-app && iga pages deploy --name my-app
```

### Existing Project (already linked)
```shell
iga pages deploy
```

### Link (without deploying)
```shell
iga pages link
```

### Local Dev (required when `api/` exists)
```shell
iga pages dev
# Serves framework + /api/* serverless functions together
```

### Build
```shell
iga pages build
```

## Deploy Behavior

- GitHub remote detected → Git deploy
- Otherwise → upload deploy (provider: upload_v2)
- Output includes preview URL with `?iga_token=...&iga_time=...` — share the full URL with query params

## Anti-Patterns

- Do NOT run iga commands outside the project root
- Do NOT deploy without logging in first
- Do NOT commit `.iga/` — it's auto-gitignored
- With GitHub remote + `provider: "upload_v2"` → delete `.iga/project.json` and redeploy to switch to Git deploy
- Do NOT use `npm run dev` / `vite` / `next dev` if `api/` exists — use `iga pages dev` instead
- Do NOT set `package.json "scripts.dev"` to `iga pages dev` (creates infinite loop); keep it as the framework's own dev command
