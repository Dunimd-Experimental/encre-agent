---
name: mcp-builder
description: Build high-quality MCP (Model Context Protocol) servers connecting LLMs with external APIs and services, with comprehensive workflow from research to evaluation.
---

# MCP Builder Skill

Create MCP servers that enable LLMs to interact with external services through well-designed tools.

## Process: 4 Phases

### Phase 1: Deep Research & Planning

**Modern MCP Design Principles:**
- **API Coverage vs Workflow Tools**: Balance comprehensive API endpoints with specialized workflow tools. When uncertain, prioritize comprehensive coverage.
- **Tool Naming**: Use consistent prefixes (e.g., `github_create_issue`, `github_list_repos`) and action-oriented names.
- **Context Management**: Design tools returning focused data; use filtering/pagination.
- **Error Messages**: Guide agents toward solutions with specific suggestions.

**Study MCP Protocol:**
- Start with sitemap: `https://modelcontextprotocol.io/sitemap.xml`
- Fetch specific pages with `.md` suffix (e.g., `https://modelcontextprotocol.io/specification/draft.md`)
- Key areas: spec overview, transport (streamable HTTP, stdio), tool/resource/prompt definitions

**Recommended Stack:**
- Language: TypeScript (strong SDK, good AI generation compatibility)
- Transport: Streamable HTTP (remote), stdio (local)

**Study Framework:**
- MCP Best Practices: https://modelcontextprotocol.io/docs/concepts/architecture
- TypeScript SDK: https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/main/README.md
- Python SDK: https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md

**Plan Implementation:**
- Review API documentation for endpoints, auth, data models
- Prioritize comprehensive API coverage
- List endpoints starting with most common operations

### Phase 2: Implementation

**Project Setup:**
- TypeScript: proper package.json, tsconfig.json
- Python: module organization, dependencies

**Core Infrastructure:**
- API client with authentication
- Error handling helpers
- Response formatting (JSON/Markdown)
- Pagination support

**For Each Tool:**

| Element | TypeScript | Python |
|---------|-----------|--------|
| Input Schema | Zod with constraints/descriptions | Pydantic models |
| Output Schema | outputSchema / structuredContent | Define where possible |
| Description | Concise summary with parameter descriptions | Same |
| Implementation | Async/await, error handling, pagination | Same |

**Annotations:** `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`

### Phase 3: Review & Test

**Code Quality:** No DRY violations, consistent errors, full type coverage, clear descriptions.

**Build & Test:**
- TypeScript: `npm run build`
- Python: `python -m py_compile your_server.py`
- Test with MCP Inspector: `npx @modelcontextprotocol/inspector`

### Phase 4: Create Evaluations

Create 10 evaluation questions (XML format) testing whether LLMs can effectively use the MCP server.

**Requirements per question:** Independent, read-only, complex (multi-tool), realistic, verifiable (single clear answer), stable.

**Output format:**
```xml
<evaluation>
  <qa_pair>
    <question>...</question>
    <answer>...</answer>
  </qa_pair>
</evaluation>
```

## Reference Files

Load these during development:

- **MCP Protocol**: `https://modelcontextprotocol.io/sitemap.xml` → fetch `.md` pages
- **Best Practices**: Core guidelines for naming, response format, pagination, transport, security
- **Python SDK**: `https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md`
- **TypeScript SDK**: `https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/main/README.md`

## Key URLs

| Resource | URL |
|----------|-----|
| MCP Protocol Spec | https://modelcontextprotocol.io/specification/draft.md |
| TypeScript SDK README | https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/main/README.md |
| Python SDK README | https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/README.md |
| MCP Inspector | npx @modelcontextprotocol/inspector |
