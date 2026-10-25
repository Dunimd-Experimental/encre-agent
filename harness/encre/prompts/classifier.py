#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""
Lightweight intent and register classifier for dynamic prompt assembly.

Two orthogonal judgements are made here because the prompt system needs two
orthogonal axes:

* **intent** (``coding`` / ``research`` / ``data``) -- *which domain* the request
  belongs to.  Drives conditional prompt blocks and tool-set expansion.
* **register** (``chat`` / ``answer`` / ``work``) -- *what shape the reply
  takes*.  Drives which behavioural blocks are in force at all: a greeting must
  not inherit the delivery standard of an implementation request, and an
  implementation request must not be answered as conversation.

Every vocabulary below carries both Chinese and English markers.  Matching only
English made every non-English request land in the wrong bucket: a short Chinese
task was read as small talk, and the domain intent -- together with the tool set
that intent unlocks -- was dropped with it.
"""


# ── Domain vocabularies (axis: intent) ───────────────────────────────

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "coding": [
        # English
        "code",
        "function",
        "class",
        "import",
        "def ",
        "async ",
        "await ",
        "bug",
        "fix",
        "refactor",
        "test",
        "compile",
        "build",
        "deploy",
        "python",
        "javascript",
        "typescript",
        "rust",
        "go ",
        "golang",
        "react",
        "vue",
        "angular",
        "node",
        "express",
        "django",
        "flask",
        "api",
        "endpoint",
        "route",
        "component",
        "hook",
        "state",
        "css",
        "html",
        "style",
        "layout",
        "responsive",
        "database",
        "sql",
        "query",
        "schema",
        "migration",
        "docker",
        "container",
        "kubernetes",
        "ci/cd",
        "pipeline",
        "git",
        "commit",
        "branch",
        "merge",
        "rebase",
        "repo",
        "pull request",
        "pr",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        "script",
        "module",
        "package",
        "dependency",
        "npm",
        "pip",
        "lsp",
        "language server",
        "autocomplete",
        "diagnostics",
        "error",
        "exception",
        "traceback",
        "stack trace",
        "file_read",
        "file_edit",
        "file_write",
        "grep",
        "glob",
        # Chinese
        "代码",
        "函数",
        "方法",
        "类",
        "接口",
        "报错",
        "错误",
        "异常",
        "缺陷",
        "修复",
        "调试",
        "重构",
        "编译",
        "构建",
        "部署",
        "测试",
        "用例",
        "仓库",
        "分支",
        "提交",
        "合并",
        "依赖",
        "模块",
        "脚本",
        "数据库",
        "建表",
        "迁移",
        "前端",
        "后端",
        "服务端",
        "项目",
        "工程",
        "源码",
        "架构",
        "实现",
        "联调",
        "发版",
        "灰度",
        "回滚",
    ],
    "research": [
        # English
        "search",
        "find",
        "look up",
        "research",
        "what is",
        "what are",
        "how to",
        "how do",
        "why does",
        "explain",
        "tell me about",
        "information",
        "news",
        "article",
        "documentation",
        "docs",
        "wiki",
        "latest",
        "update",
        "trend",
        "compare",
        "difference",
        "definition",
        "meaning",
        "example",
        "tutorial",
        "web search",
        "web_search",
        "web fetch",
        "web_fetch",
        "browse",
        "url",
        "http",
        "https",
        "website",
        "page",
        "paper",
        "publication",
        "reference",
        "source",
        # Chinese
        "搜索",
        "查询",
        "查找",
        "查一下",
        "找一下",
        "研究",
        "调研",
        "什么是",
        "是什么",
        "为什么",
        "如何",
        "怎么",
        "解释",
        "介绍",
        "讲讲",
        "说明",
        "资料",
        "文档",
        "手册",
        "新闻",
        "资讯",
        "最新",
        "趋势",
        "对比",
        "区别",
        "定义",
        "意思",
        "例子",
        "教程",
        "网址",
        "网页",
        "论文",
        "出处",
        "来源",
        "了解",
    ],
    "data": [
        # English
        "data",
        "csv",
        "excel",
        "spreadsheet",
        ".csv",
        ".xlsx",
        ".xls",
        "notebook",
        "jupyter",
        ".ipynb",
        "pandas",
        "matplotlib",
        "analysis",
        "analyze",
        "visualize",
        "chart",
        "graph",
        "plot",
        "statistics",
        "statistical",
        "regression",
        "correlation",
        "dataset",
        "dataframe",
        "column",
        "row",
        "table",
        "metrics",
        "pdf",
        ".pdf",
        "document",
        "extract",
        "image",
        "photo",
        "picture",
        ".png",
        ".jpg",
        ".jpeg",
        "screenshot",
        "scatter",
        "histogram",
        "bar chart",
        "dashboard",
        # Chinese
        "数据",
        "表格",
        "电子表格",
        "图表",
        "统计",
        "分析",
        "指标",
        "可视化",
        "报表",
        "看板",
        "分布",
        "占比",
        "样本",
        "字段",
        "提取",
        "识别",
        "截图",
        "图片",
        "照片",
    ],
}

# ── Register vocabularies (axis: register) ───────────────────────────

#: The user expects something to exist at the end of the turn: a file, a
#: change, a built thing, a finished result.  Strongest ``work`` signal.
_PRODUCTION_MARKERS: list[str] = [
    # English
    "write",
    "create",
    "make",
    "build",
    "generate",
    "implement",
    "add ",
    "remove",
    "delete",
    "rename",
    "move ",
    "copy",
    "update",
    "change",
    "modify",
    "edit",
    "patch",
    "fix",
    "refactor",
    "optimi",
    "integrate",
    "set up",
    "setup",
    "install",
    "configure",
    "migrate",
    "deploy",
    "release",
    "publish",
    "export",
    "convert",
    "translate",
    "summarise",
    "summarize",
    "organize",
    "organise",
    "draft",
    "produce",
    "ship",
    "finish",
    "complete",
    "carry out",
    "execute",
    "scaffold",
    "package",
    "speak",
    "give me",
    "file",
    "folder",
    "directory",
    "document",
    "report",
    "script",
    "program",
    "app",
    "website",
    "test",
    "commit",
    "readme",
    "spreadsheet",
    "slides",
    "diagram",
    "poster",
    "plan",
    # Chinese
    "写",
    "创建",
    "新建",
    "做个",
    "做一个",
    "生成",
    "实现",
    "添加",
    "增加",
    "删除",
    "移除",
    "重命名",
    "改名",
    "移动",
    "复制",
    "更新",
    "修改",
    "调整",
    "补全",
    "修复",
    "重构",
    "优化",
    "整合",
    "集成",
    "安装",
    "配置",
    "部署",
    "上线",
    "发布",
    "导出",
    "导入",
    "转换",
    "翻译",
    "总结",
    "汇总",
    "整理",
    "梳理",
    "起草",
    "做完",
    "完成",
    "交付",
    "产出",
    "跑一遍",
    "跑起来",
    "搭建",
    "打包",
    "封装",
    "沉淀",
    "做成",
    "改造",
    "接入",
    "对接",
    "迁移",
    "给我",
    "帮我",
    "文件",
    "目录",
    "文档",
    "报告",
    "脚本",
    "程序",
    "应用",
    "网站",
    "表格",
    "清单",
    "方案",
    "计划",
    "设计",
    "原型",
    "图表",
    "海报",
    "技能",
    "插件",
    "接口",
    "服务",
]

#: The user wants something looked over, read or mapped.  Delivery still
#: applies -- a survey has to be completed, not begun.
_INSPECTION_MARKERS: list[str] = [
    "look at",
    "check",
    "review",
    "inspect",
    "examine",
    "explore",
    "survey",
    "audit",
    "go through",
    "read through",
    "walk me through",
    "map out",
    "how does",
    "how is",
    "what does",
    "where is",
    "where are",
    "which files",
    # Chinese
    "看看",
    "看一下",
    "查看",
    "检查",
    "审",
    "过一遍",
    "盘一下",
    "读一下",
    "通读",
    "浏览",
    "排查",
    "定位",
    "在哪",
    "哪里",
    "哪些文件",
    "怎么实现",
    "如何实现",
    "实现逻辑",
    "看一下项目",
    "熟悉",
]

#: The user wants an explanation or a status, not a produced artefact.
_QUESTION_MARKERS: list[str] = [
    # English
    "what is",
    "what are",
    "who is",
    "why",
    "how do",
    "how to",
    "can you",
    "explain",
    "difference between",
    "meaning of",
    "define",
    "is it possible",
    "should i",
    "what should",
    "do you think",
    # Chinese
    "什么是",
    "是什么",
    "为什么",
    "为何",
    "怎么",
    "如何",
    "可不可以",
    "能不能",
    "有没有",
    "解释",
    "含义",
    "意思",
    "区别",
    "原理",
    "作用",
    "用途",
    "你觉得",
    "怎么办",
    "哪个好",
    "是不是",
    "是什么情况",
]

#: Greetings and acknowledgements.  Never a task on their own.
_SOCIAL_MARKERS: list[str] = [
    # English
    "hi",
    "hello",
    "hey",
    "yo",
    "sup",
    "howdy",
    "good morning",
    "good night",
    "how are you",
    "how's it going",
    "what's up",
    "thanks",
    "thank you",
    "ty",
    "ok",
    "okay",
    "cool",
    "nice",
    "haha",
    "lol",
    "yeah",
    "nope",
    "bye",
    "see you",
    "good job",
    "well done",
    "sorry",
    "never mind",
    "nvm",
    # Chinese
    "你好",
    "您好",
    "哈喽",
    "嗨",
    "在吗",
    "在不在",
    "在么",
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "晚安",
    "早",
    "谢谢",
    "多谢",
    "感谢",
    "好的",
    "行",
    "可以",
    "哈哈",
    "呵呵",
    "没事",
    "算了",
    "走了",
    "再见",
    "辛苦了",
    "厉害",
    "不错",
    "无聊",
    "困",
]

#: Affect-led or digressive shapes.  These stay conversation even when long --
#: the failure mode being fixed is a 40-character Chinese sentence about the
#: user's day being treated as a work order.
_CONVERSATIONAL_SHAPES: list[str] = [
    # English
    "i think",
    "i feel",
    "i'm tired",
    "im tired",
    "guess what",
    "just wondering",
    "random thought",
    "btw",
    "by the way",
    "anyway",
    # Chinese
    "我觉得",
    "我感觉",
    "我想着",
    "随便聊聊",
    "随便说说",
    "闲聊",
    "吐槽",
    "我跟你说",
    "你知道吗",
    "对了",
    "顺便",
    "其实",
    "反正",
    "有点",
    "挺",
    "蛮",
    "累",
    "烦",
    "开心",
    "好奇",
    "今天",
    "昨天",
    "刚才",
]


def _hit(text: str, markers: list[str]) -> bool:
    """Return True when any marker occurs in *text*."""
    return any(marker in text for marker in markers)


def _count_hits(text: str, markers: list[str]) -> int:
    """Return how many markers occur in *text*."""
    return sum(1 for marker in markers if marker in text)


def _looks_social(text: str) -> bool:
    """True for greetings, acknowledgements and affect-led conversation."""
    return _hit(text, _SOCIAL_MARKERS) or _hit(text, _CONVERSATIONAL_SHAPES)


def _is_conversational(query: str) -> bool:
    """Heuristic: greetings and small talk carry no domain request.

    Kept for existing call sites.  Length alone never made a message a task,
    and length alone must not make one a greeting.
    """
    stripped = (query or "").strip()
    if not stripped:
        return True
    text = stripped.lower()
    if len(stripped) <= 3 or _hit(text, _SOCIAL_MARKERS):
        return True
    if _hit(text, _PRODUCTION_MARKERS) or _hit(text, _INSPECTION_MARKERS):
        return False
    if _hit(text, _CONVERSATIONAL_SHAPES):
        return True
    return (
        not _hit(text, _DOMAIN_KEYWORDS["coding"])
        and not _hit(text, _DOMAIN_KEYWORDS["research"])
        and not _hit(text, _DOMAIN_KEYWORDS["data"])
    )


def classify_intents(query: str) -> list[str]:
    """Classify a user query into domain intents for prompt assembly.

    Returns labels such as ``["general", "coding"]``.  ``conversation`` is added
    for greeting-shaped messages but never *replaces* a domain label: an earlier
    version returned early on the conversational judgement, which silently
    dropped ``coding`` -- and with it the coding tool set -- from any request
    phrased outside the English vocabulary.
    """
    intents: set[str] = {"general"}
    stripped = (query or "").strip()
    if not stripped:
        return ["conversation", "general"]

    text = stripped.lower()
    if _looks_social(text) and not _hit(text, _PRODUCTION_MARKERS):
        intents.add("conversation")

    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if _hit(text, keywords):
            intents.add(domain)

    return sorted(intents)


def classify_register(
    query: str,
    *,
    workspace_root: str = "",
) -> str:
    """Resolve the response register for one turn: ``chat`` / ``answer`` / ``work``.

    The axes stay independent: ``classify_intents`` says which domain, this
    says what shape.  A coding question inside a workspace is still only a
    question (``answer``), and "thanks, that's all" inside a workspace is
    still conversation (``chat``).

    Ordering follows evidence strength:

    1. production markers -> ``work``;
    2. inspection markers -> ``work`` (a survey must be finished, not begun);
    3. social/affective shape with nothing to produce -> ``chat``;
    4. question shape -> ``answer``;
    5. unrecognised -> ``work`` inside a workspace, ``answer`` outside one
       (short unrecognised tokens stay conversation).

    Args:
        query: The user prompt for this turn.
        workspace_root: Non-empty when the session is bound to a project
            workspace; raises the ambiguous default to ``work``.

    Returns:
        One of ``"chat"``, ``"answer"``, ``"work"``.
    """
    stripped = (query or "").strip()
    if not stripped:
        return "chat"

    text = stripped.lower()
    production = _count_hits(text, _PRODUCTION_MARKERS)
    inspection = _count_hits(text, _INSPECTION_MARKERS)
    question = _count_hits(text, _QUESTION_MARKERS)
    social = _looks_social(text)

    if production or inspection:
        return "work"
    if social and not question:
        return "chat"
    if question:
        return "answer"
    if len(stripped) <= 12:
        return "chat"
    return "work" if workspace_root else "answer"
