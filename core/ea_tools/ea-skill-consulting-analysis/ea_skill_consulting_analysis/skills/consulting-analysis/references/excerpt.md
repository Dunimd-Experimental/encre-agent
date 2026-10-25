
## Analysis Framework

### Chapter 1: [Title]
- **Analysis Objective**: [This chapter aims to...]
- **Analysis Logic**: [Framework or reasoning chain used]
- **Core Hypothesis**: [Hypotheses to validate]
- **Data Requirements**: (see Step 1.4)
- **Visualization Plan**: (see Step 1.5)

### Chapter 2: [Title]
...
```

### Step 1.4: Define Data Query Requirements Per Chapter

For each chapter, specify **exactly what data needs to be collected**. This is the bridge to downstream data collection skills.

Each data requirement entry must include:

| Field | Description |
|-------|-------------|
| **Data Metric** | The specific metric or data point needed (e.g., "China skincare market size 2020-2025 (in billion CNY)") |
| **Data Type** | Quantitative, Qualitative, or Mixed |
| **Suggested Sources** | Suggested source categories: Industry reports, financial statements, government statistics, social media, e-commerce platforms, survey data, news |
| **Search Keywords** | Suggested search queries for data collection agents |
| **Priority** | P0 (Required) / P1 (Important) / P2 (Supplementary) |
| **Time Range** | The time period the data should cover |

#### Data Requirements Output Format (per chapter)

```markdown
#### Data Requirements

| # | Data Metric | Data Type | Suggested Sources | Search Keywords | Priority | Time Range |
|---|-------------|-----------|-------------------|-----------------|----------|------------|
| 1 | Market size (billion CNY) | Quantitative | Industry reports, government statistics | "China skincare market size 2024" | P0 | 2020-2025 |
| 2 | CAGR | Quantitative | Industry reports | "skincare CAGR growth rate" | P0 | 2020-2025 |
| 3 | Sub-category share | Quantitative | E-commerce platforms, industry reports | "skincare category share cream serum sunscreen" | P1 | Latest |
| 4 | Policy & regulatory updates | Qualitative | Government announcements, news | "cosmetics regulation 2024" | P2 | Past 1 year |
```

### Step 1.5: Define Visualization & Content Structure Per Chapter

For each chapter, specify the **planned visualization** and **content structure** for the final report:

| Field | Description |
|-------|-------------|
| **Visualization Type** | Chart type: Line chart, bar chart, pie chart, scatter plot, radar chart, heatmap, Sankey diagram, comparison table, etc. |
| **Visualization Title** | Descriptive title for the chart |
| **Visualization Data Mapping** | Which data indicators map to X/Y axes or segments |
| **Comparison Table Design** | Column headers and comparison dimensions for the data contrast table |
| **Argument Structure** | The planned "What → Why → So What" narrative outline |

#### Visualization Plan Output Format (per chapter)

```markdown
#### Visualization & Content Plan

**Chart 1**: [Type] — [Title]
- X-axis: [Dimension], Y-axis: [Metric]
- Data source: Corresponds to Data Requirement #1, #2

**Comparison Table**:
| Dimension | Item A | Item B | Item C |
|-----------|--------|--------|--------|

**Argument Structure**:
1. **Observation (What)**: [Surface phenomenon revealed by data]
2. **Attribution (Why)**: [Driving factors or underlying causes]
3. **Implication (So What)**: [Strategic implications or recommended actions]
```

### Step 1.6: Output Complete Analysis Framework

Assemble all outputs into a single, structured **Analysis Framework Document**:

```markdown
# [Research Subject] Analysis Framework

## Research Overview
- **Research Subject**: [...]
- **Scope**: [Geography, time range, industry segment]
- **Analysis Domain**: [Market / Finance / Industry / Brand / Consumer / ...]
- **Core Research Questions**: [1-3 key questions]

## Framework Selection

| Chapter | Selected Framework(s) | Application |
|---------|----------------------|-------------|
| ... | ... | ... |

## Chapter Skeleton

### 1. [Chapter Title]
- **Analysis Objective**: [...]
- **Analysis Logic**: [...]
- **Core Hypothesis**: [...]

#### Data Requirements
| # | Data Metric | Data Type | Suggested Sources | Search Keywords | Priority | Time Range |
|---|-------------|-----------|-------------------|-----------------|----------|------------|
| ... | ... | ... | ... | ... | ... | ... |

#### Visualization & Content Plan
[Chart plan + Comparison table design + Argument structure]

### 2. [Chapter Title]
...

### N. [Chapter Title]
...

## Data Collection Task List
[Consolidate all P0/P1 data requirements across chapters into a structured task list for downstream data collection skills to execute]
```

## Phase 1 Quality Checklist

- [ ] Analysis framework covers all natural dimensions for the identified domain
- [ ] 2-4 professional analysis frameworks are selected and explicitly mapped to chapters
- [ ] Selected frameworks are complementary (not overlapping) and data-feasible
- [ ] Each chapter has clear Analysis Objective, Analysis Logic (referencing chosen framework), and Core Hypothesis
- [ ] Data requirements are specific, measurable, and include search keywords
- [ ] Every chapter has at least one visualization plan
- [ ] Data priorities (P0/P1/P2) are assigned realistically
- [ ] The framework is actionable — a data collection agent can execute on the Search Keywords directly
- [ ] Data Collection Task List is comprehensive and deduplicated

---

# Phase 1→2 Handoff: Data Collection & Chart Generation

After the analysis framework is generated, it is handed off to **other data collection skills** (e.g., deep-research, data-analysis, web search agents) to:

1. Execute the **Search Keywords** from each chapter's data requirements
2. Collect quantitative data, qualitative insights, and source URLs
3. Generate charts based on the **Visualization & Content Plan**
4. Return a **Data Package** containing:
   - **Data Summary**: Raw numbers, metrics, and qualitative findings per chapter
   - **Chart Files**: Generated chart images with local file paths
   - **External Search Findings**: Source URLs and summaries for citations

> **This skill does NOT perform data collection.** It only produces the framework (Phase 1) and the final report (Phase 2).
>
> **Chart Generation**: If a visualization/charting skill is available (e.g., data-analysis, image-generation), chart generation can be deferred to the beginning of Phase 2 — see Step 2.3.

---

# Phase 2: Report Generation

## Purpose

Receive the completed **Analysis Framework** and **Data Package** from upstream, and synthesize them into a final consulting-grade report.

## Phase 2 Inputs

| Input | Description | Required |
|-------|-------------|----------|
| **Analysis Framework** | The framework document produced in Phase 1 | Yes |
| **Data Summary** | Collected data organized per chapter from the data collection phase | Yes |
| **Chart Files** | Local file paths for generated chart images. If not provided, will be generated in Step 2.3 using available visualization skills | Optional |
| **External Search Findings** | URLs and summaries for inline citations | Optional |

## Phase 2 Workflow

### Step 2.1: Receive and Validate Inputs

Verify that all required inputs are present:

1. **Analysis Framework** — Confirm it contains chapter skeleton, data requirements, and visualization plans
2. **Data Summary** — Confirm it contains data organized per chapter, cross-reference against P0 requirements
3. **Chart Files** — Confirm file paths are valid local paths

If any P0 data is missing, note it in the report and flag for the user.

### Step 2.2: Map Report Structure

Map the final report structure from the Analysis Framework:

1. **Abstract** — Executive summary with key takeaways
2. **Introduction** — Background, objectives, methodology
3. **Main Body Chapters (2...N)** — Mapped from the Framework's chapter skeleton
4. **Conclusion** — Pure, objective synthesis
5. **References** — GB/T 7714-2015 formatted references

### Step 2.3: Generate Chapter Charts (Pre-Report Visualization)

Before writing the report, generate all planned charts from the Analysis Framework's **Visualization & Content Plan**. This step ensures every sub-chapter has its "Visual Anchor" ready before narrative writing begins.

#### When to Execute This Step

- **Chart Files already provided**: Skip this step — proceed directly to Step 2.4.
- **Chart Files NOT provided but a visualization skill is available**: Execute this step to generate all charts first.
- **No Chart Files and no visualization skill available**: Skip this step — use comparison tables as the primary visual anchor in Step 2.4, and note the absence of charts.

#### Chart Generation Workflow

1. **Extract Chart Tasks**: Parse all `Visualization & Content Plan` entries from the Analysis Framework to build a chart generation task list:

| # | Chapter | Chart Type | Chart Title | Data Mapping | Data Source |
|---|---------|------------|-------------|--------------|-------------|
| 1 | 2.1 | Line chart | Market Size Trend 2020-2025 | X: Year, Y: Market Size (billion CNY) | Data Requirement #1, #2 |
| 2 | 3.1 | Pie chart | Consumer Age Distribution | Segments: Age groups, Values: Share % | Data Requirement #5 |
| ... | ... | ... | ... | ... | ... |

2. **Prepare Chart Data**: For each chart task, extract the corresponding data points from the **Data Summary**.
   > **CRITICAL**: Use ONLY the numbers provided in the Data Summary. Do NOT invent or "smooth" data to make charts look better. If data points are missing, the chart must reflect that reality (e.g., broken line or missing bar), or the chart type must be adjusted.

3. **Delegate to Visualization Skill**: Invoke the available visualization/charting skill (e.g., `data-analysis`) for each chart task with:
   - Chart type and title
   - Structured data
   - Axis labels and formatting preferences
   - Output file path convention: `charts/chapter_{N}_{chart_index}.png`

4. **Collect Chart File Paths**: Record all generated chart file paths for embedding in Step 2.4:

```markdown
## Generated Charts
| # | Chapter | Chart Title | File Path |
|---|---------|-------------|-----------|
| 1 | 2.1 | Market Size Trend 2020-2025 | charts/chapter_2_1.png |
| 2 | 3.1 | Consumer Age Distribution | charts/chapter_3_1.png |
```

5. **Validate**: Confirm all P0-priority charts have been generated. If any chart generation fails, note it and fall back to comparison tables for that sub-chapter.

> **Principle**: Complete ALL chart generation before starting report writing. This ensures a consistent visual narrative and avoids interleaving generation with writing.

### Step 2.4: Write the Report

For each sub-chapter, follow the **"Visual Anchor → Data Contrast → Integrated Analysis"** flow:

1. **Visual Evidence Block**: Embed charts using `![Image Description](Actual_File_Path)` — use the file paths collected in Step 2.3
2. **Data Contrast Table**: Create a Markdown comparison table for key metrics
   > **Source Rule**: Every number in the table must come from the Data Summary. No hallucinations.
3. **Integrated Narrative Analysis**: Write analytical text following "What → Why → So What"
   > **Narrative Rule**: Narrative must explain the *provided* data. Do not make claims unsupported by the inputs.

Each sub-chapter must end with a robust analytical paragraph (min. 200 words) that:
- Synthesizes conflicting or reinforcing data points
- Reveals the underlying user tension or opportunity
- Optionally ends with a punchy "One-Liner Truth" in a blockquote (`>`)

### Step 2.5: Final Structure Self-Check

Before outputting, confirm the report contains **all sections in order**:

```
Abstract → 1. Introduction → 2...N. Body Chapters → N+1. Conclusion → N+2. References
```

Additionally verify:
- All charts generated in Step 2.3 are embedded in the correct sub-chapters
- Chart file paths in `![](path)` references are valid
- Sub-chapters without charts have comparison tables as visual anchors

The report **MUST NOT** stop after the Conclusion — it **MUST** include References as the final section.

## Formatting & Tone Standards

### Consulting Voice
- **Tone**: McKinsey/BCG — Authoritative, Objective, Professional
- **Language**: All headings and content in the language specified by `output_locale`
- **Number Formatting**: Use English commas for thousands separators (`1,000` not `1，000`)
- **Data emphasis**: **Bold** important viewpoints and key numbers

### Titling Constraints
- **Numbering**: Use standard numbering (`1.`, `1.1`) directly followed by the title
- **Forbidden Prefixes**: Do NOT use "Chapter", "Part", "Section" as prefixes
- **Allowed Tone Words**: Analysis, Profiling, Overview, Insights, Assessment
- **Forbidden Words**: "Decoding", "DNA", "Secrets", "Mindscape", "Solar System", "Unlocking"

### Sub-Chapter Conclusions
- **Requirement**: End each sub-chapter with a robust analytical paragraph (min. 200 words).
- **Narrative Flow**: This paragraph must look like a natural continuation of the text. It must synthesize the section's findings into a strategic judgment.
- **Content Logic**:
    1.  Synthesize the conflicting or reinforcing data points above.
    2.  Reveal the *underlying* user tension or opportunity.
    3.  Key Insight: **Optional**: Only if you have a concise, punchy "One-Liner Truth", place it at the very end using a **Blockquote** (`>`) to anchor the section.

### Insight Depth (The "So What" Chain)

Every insight must connect **Data → User Psychology → Strategy Implication**:

```
❌ Bad: "Females are 60%. Strategy: Target females."

✅ Good: "Females constitute 60% with a high TGI of 180. **This suggests**
   the purchase decision is driven by aesthetic and social validation
   rather than pure utility. **Consequently**, media spend should pivot
   towards visual-heavy platforms (e.g., RED/Instagram) to maximize CTR,
   treating male audiences only as a secondary gift-giving segment."
```

### References
- **Inline**: Use markdown links for sources (e.g. `[Source Title](URL)`) when using External Search Findings
- **References section**: Formatted strictly per **GB/T 7714-2015**

### Markdown Rules
- **Immediate Start**: Begin directly with `# Report Title` — no introductory text
- **No Separators**: Do NOT use horizontal rules (`---`)

## Report Structure Template

```markdown
# [Report Title]

## Abstract
[Executive summary with key takeaways]

## 1. Introduction
[Background, objectives, methodology]

## 2. [Body Chapter Title]
### 2.1 [Sub-chapter Title]
![Chart Description](chart_file_path)

| Metric | Brand A | Brand B |
|--------|---------|--------|
| ... | ... | ... |

[Integrated narrative analysis: What → Why → So What, min. 200 words]

> [Optional: One-liner strategic truth]

### 2.2 [Sub-chapter Title]
...

## N+1. Conclusion
[Pure objective synthesis, NO bullet points, neutral tone]
[Para 1: The fundamental nature of the group/market]
[Para 2: Core tension or behavior pattern]
[Final: One or two sentences stating the objective truth]

## N+2. References
[1] Author. Title[EB/OL]. URL, Date.
[2] ...
```

## Complete Example

### Phase 1 Example: Framework Generation

User provides: Research subject "Gen-Z Skincare Market Analysis"

**Phase 1 output (Analysis Framework):**

```markdown
# Gen-Z Skincare Market Analysis Framework

## Research Overview
- **Research Subject**: Gen-Z Skincare Market Deep Analysis
- **Scope**: China market, 2020-2025, consumers aged 18-27
- **Analysis Domain**: Market Analysis + Consumer Insights
- **Core Research Questions**:
  1. What is the size and growth momentum of the Gen-Z skincare market?
  2. What is unique about Gen-Z consumer skincare behavior patterns?
  3. How can brands effectively reach and convert Gen-Z consumers?

## Chapter Skeleton

### 1. Market Size & Growth Trends
- **Analysis Objective**: Quantify Gen-Z skincare market size and identify growth drivers
- **Analysis Logic**: Total market → Segmentation → Growth rate → Driver decomposition
- **Core Hypothesis**: Gen-Z is becoming the core engine of skincare consumption growth

#### Data Requirements
| # | Data Metric | Data Type | Suggested Sources | Search Keywords | Priority | Time Range |
|---|-------------|-----------|-------------------|-----------------|----------|------------|
| 1 | China skincare market total size | Quantitative | Industry reports | "China skincare market size 2024 2025" | P0 | 2020-2025 |
| 2 | Gen-Z skincare spending share | Quantitative | Industry reports, e-commerce platforms | "Gen-Z skincare spending share youth" | P0 | Latest |

#### Visualization & Content Plan
**Chart 1**: Line chart — China Skincare Market Size Trend 2020-2025
**Argument Structure**:
1. What: Quantified status of market size and Gen-Z share
2. Why: Consumption upgrade, ingredient-conscious consumers, social media driven
3. So What: Brands should prioritize building youth-oriented product lines

### 2. Consumer Profiling & Behavioral Insights
...

## Data Collection Task List
[Consolidated P0/P1 tasks]
```

### Phase 2 Example: Report Generation

After data collection, user provides: Analysis Framework + Data Summary with brand metrics + chart file paths.

**Phase 2 output (Final Report) follows this flow:**

1. Start with `# Gen-Z Skincare Market Deep Analysis Report`
2. Abstract — 3-5 key takeaways in executive summary form
3. 1. Introduction — Market context, research scope, data sources
4. 2. Market Size & Growth Trend Analysis — Embed trend charts, comparison tables, strategic narrative
5. 3. Consumer Profiling & Behavioral Insights — Demographics, purchase drivers, "So What" analysis
6. 4. Brand Competitive Landscape Assessment — Brand positioning, share analysis, competitive dynamics
7. 5. Marketing Strategy & Channel Insights — Channel effectiveness, content strategy implications
8. 6. Conclusion — Objective synthesis in flowing prose (no bullets)
9. 7. References — GB/T 7714-2015 formatted list

---

## Quality Checklists

### Phase 1 Quality Checklist (Analysis Framework)

- [ ] Framework covers all natural analytical dimensions for the identified domain
- [ ] Each chapter has clear Analysis Objective, Analysis Logic, and Core Hypothesis
- [ ] Data requirements are specific, measurable, and include actionable Search Keywords
- [ ] Every chapter has at least one visualization plan with chart type and data mapping
- [ ] Data priorities (P0/P1/P2) are assigned — P0 items are essential for core arguments
- [ ] Data Collection Task List is comprehensive, deduplicated, and ready for downstream execution
- [ ] Framework adapts to the correct domain (market/finance/industry/consumer/etc.)

### Phase 2 Quality Checklist (Final Report)

- [ ] **NO HALLUCINATION**: All numbers and charts are verified against the input Data Summary
- [ ] All planned charts generated before report writing (Step 2.3 completed first)
- [ ] All sections present in correct order (Abstract → Introduction → Body → Conclusion → References)
- [ ] Every sub-chapter follows "Visual Anchor → Data Contrast → Integrated Analysis"
- [ ] Every sub-chapter ends with a min. 200-word analytical paragraph
- [ ] All insights follow the "Data → User Psychology → Strategy Implication" chain
- [ ] All headings use proper numbering (no "Chapter/Part/Section" prefixes)
- [ ] Charts are embedded with `![Description](path)` syntax
- [ ] Numbers use English commas for thousands separators
- [ ] Inline references use markdown links where applicable
- [ ] References section follows GB/T 7714-2015
- [ ] No horizontal rules (`---`) in the document
- [ ] Conclusion uses flowing prose — no bullet points
- [ ] Report starts directly with `#` title — no preamble
- [ ] Missing P0 data is explicitly flagged in the report

## Output Format

- **Phase 1**: Output the complete Analysis Framework in **Markdown** format
- **Phase 2**: Output the complete Report in **Markdown** format

## Settings

```
output_locale = zh_CN  # configurable per user request
reasoning_locale = en
```

## Notes

- This skill operates in **two phases** of a multi-step agentic workflow:
  - **Phase 1** produces the analysis framework and data collection requirements
  - **Data collection** is performed by other skills (deep-research, data-analysis, etc.)
  - **Phase 2** receives the collected data and produces the final report
- Dynamic titling: **Rewrite** topics from the Framework into professional, concise subject-based headers
- The Conclusion section must contain **NO** detailed recommendations — those belong in the preceding body chapters
- **ZERO HALLUCINATION POLICY**: Each statement, chart, and number in the report must be supported by data points from the input Data Summary. If data is missing, admit it.
- **Traceability**: If requested, you must be able to point to the specific line in the Data Summary or External Search Findings that supports a claim.
- The framework should adapt its analytical dimensions and depth to the specific domain (financial analysis uses different frameworks than consumer insights)
- When the research subject is ambiguous, default to the broadest reasonable scope and note assumptions
