import { defineConfig } from 'vitepress'

const nav_zh = [
  { text: 'Encre Agent Harness', link: '/harness', activeMatch: '^(harness|en/harness)' },
  { text: 'Encre Agent Core', link: '/core', activeMatch: '^(core|en/core)' },
  { text: 'Encre Agent Desktop', link: '/desktop/', activeMatch: '^(?!.*(harness|core)).*$' }
]

const nav_en = [
  { text: 'Encre Agent Harness', link: '/en/harness', activeMatch: '^(harness|en/harness)' },
  { text: 'Encre Agent Core', link: '/en/core', activeMatch: '^(core|en/core)' },
  { text: 'Encre Agent Desktop', link: '/en/desktop/', activeMatch: '^(?!.*(harness|core)).*$' }
]

const sidebar_zh = [
  {
    text: '开始使用',
    items: [
      { text: '介绍', link: '/desktop/' },
      { text: '快速上手', link: '/desktop/getting-started' }
    ]
  },
  {
    text: '核心功能',
    items: [
      { text: '三种工作模式', link: '/desktop/modes' },
      { text: '对话与会话', link: '/desktop/chat' },
      { text: '工作区', link: '/desktop/workspace' },
      { text: '自动化任务', link: '/desktop/automation' },
      { text: '全局搜索', link: '/desktop/search' },
      { text: '内置工具面板', link: '/desktop/panels' }
    ]
  },
  {
    text: '设置',
    items: [
      { text: '模型管理', link: '/desktop/models' },
      { text: '智能体', link: '/desktop/agents' },
      { text: '网关接入', link: '/desktop/gateway' },
      { text: '技能与命令', link: '/desktop/skills' },
      { text: 'MCP Server', link: '/desktop/mcp' },
      { text: '规则', link: '/desktop/rules' },
      { text: '记忆', link: '/desktop/memory' },
      { text: '权限与安全', link: '/desktop/permissions' },
      { text: '通用设置', link: '/desktop/general' },
      { text: '存储与数据', link: '/desktop/storage' },
      { text: '用量统计', link: '/desktop/usage' }
    ]
  },
  {
    text: '更多',
    items: [
      { text: '快捷键参考', link: '/desktop/shortcuts' },
      { text: '常见问题', link: '/desktop/faq' },
      { text: '关于与更新', link: '/desktop/about' }
    ]
  }
]

const sidebar_en = [
  {
    text: 'Getting Started',
    items: [
      { text: 'Introduction', link: '/en/desktop/' },
      { text: 'Quick Start', link: '/en/desktop/getting-started' }
    ]
  },
  {
    text: 'Core Features',
    items: [
      { text: 'Three Modes', link: '/en/desktop/modes' },
      { text: 'Chat & Sessions', link: '/en/desktop/chat' },
      { text: 'Workspace', link: '/en/desktop/workspace' },
      { text: 'Automation', link: '/en/desktop/automation' },
      { text: 'Search', link: '/en/desktop/search' },
      { text: 'Built-in Panels', link: '/en/desktop/panels' }
    ]
  },
  {
    text: 'Settings',
    items: [
      { text: 'Models', link: '/en/desktop/models' },
      { text: 'Agents', link: '/en/desktop/agents' },
      { text: 'Gateway', link: '/en/desktop/gateway' },
      { text: 'Skills & Commands', link: '/en/desktop/skills' },
      { text: 'MCP Server', link: '/en/desktop/mcp' },
      { text: 'Rules', link: '/en/desktop/rules' },
      { text: 'Memory', link: '/en/desktop/memory' },
      { text: 'Permissions', link: '/en/desktop/permissions' },
      { text: 'General', link: '/en/desktop/general' },
      { text: 'Storage', link: '/en/desktop/storage' },
      { text: 'Usage', link: '/en/desktop/usage' }
    ]
  },
  {
    text: 'More',
    items: [
      { text: 'Shortcuts', link: '/en/desktop/shortcuts' },
      { text: 'FAQ', link: '/en/desktop/faq' },
      { text: 'About', link: '/en/desktop/about' }
    ]
  }
]

export default defineConfig({
  lang: 'zh-CN',
  title: 'Encre Agent Docs',
  description: 'Encre Agent Docs - Encre Agent 全产品官方帮助文档',
  base: '/',
  cleanUrls: false,
  head: [
    ['link', { rel: 'icon', href: '/assets/Encre.ico' }]
  ],
  themeConfig: {
    logo: { light: '/assets/Encre-lm.svg', dark: '/assets/Encre-dm.svg', alt: 'Encre' },
    siteTitle: 'Encre Agent Docs',
    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '未找到相关结果',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' }
          }
        }
      }
    },
    lastUpdated: { text: '更新于' },
    docFooter: { prev: '上一页', next: '下一页' },
    externalLinkIcon: true,
    outline: { label: '本页目录', level: [2, 3] },
    nav: nav_zh,
    sidebar: sidebar_zh,
    socialLinks: [],
    /* i18n locale switch */
    localeText: { label: 'English', link: '/en/' },
    langMenuLabel: '语言 / Language'
  },
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      themeConfig: {
        nav: nav_zh,
        sidebar: sidebar_zh,
        docFooter: { prev: '上一页', next: '下一页' },
        outline: { label: '本页目录', level: [2, 3] },
        lastUpdated: { text: '更新于' },
        langMenuLabel: '语言 / Language',
        localeText: { label: 'English', link: '/en/' }
      }
    },
    en: {
      label: 'English',
      lang: 'en',
      link: '/en/',
      themeConfig: {
        nav: nav_en,
        sidebar: sidebar_en,
        docFooter: { prev: 'Previous', next: 'Next' },
        outline: { label: 'On this page', level: [2, 3] },
        lastUpdated: { text: 'Updated' },
        langMenuLabel: 'Language',
        localeText: { label: '简体中文', link: '/' }
      }
    }
  },
  markdown: {
    lineNumbers: false,
    image: { lazyLoading: true }
  }
})