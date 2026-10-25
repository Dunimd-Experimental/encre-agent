---
name: report-generator-skill
description: Video analysis report generation skill. Integrates breakdown data and hook analysis results to generate a professional Markdown video analysis report. Run the script `python scripts/generate_report.py <breakdown_json> [hook_analysis_json]`, output a complete Markdown report including basic info, first-three-seconds hook analysis, segment overview, BGM analysis, scene analysis, and other sections.
---

# Video Analysis Report Generation

## Overview

The video analysis report generation skill integrates breakdown data and (optional) hook analysis results into a professional Markdown analysis report. The report includes basic video information, first-three-seconds hook scoring, segment overview table, BGM analysis, scene analysis, and platform recommendations.

## Use Cases

1. **Video analysis delivery**: Generate complete video analysis documentation for clients or teams
2. **Creative review**: Generate structured video content review reports
3. **Competitive reports**: Batch generate competitor video analysis reports

## Usage Steps

### Full Report (Breakdown + Hook Analysis)

```shell
# 1. Prepare breakdown data and hook analysis data (JSON files)
# 2. Generate report
python scripts/generate_report.py breakdown.json hook_analysis.json

# 3. Save to file
python scripts/generate_report.py breakdown.json hook_analysis.json > report.md
```

### Breakdown-only Report (No Hook Analysis)

```shell
python scripts/generate_report.py breakdown.json
```

## Report Structure

The generated report contains the following sections:

```markdown
# Video Analysis Report

## Basic Info
- Video duration, segment count, resolution

## First-Three-Seconds Hook Analysis (Core)
- Overall score
- 5-dimension scoring table
- Hook type
- Strengths/Weaknesses/Optimization suggestions
- Retention prediction

## Segment Overview
- Overview table of the first 10 segments

## BGM Analysis
- Music style, emotional tone, tempo

## Scene Analysis
- Main scenes, video style, target audience
- Platform recommendations

Report generation time
```

## Input Format

### breakdown.json (Required)

```json
{
  "duration": 30.5,
  "segment_count": 12,
  "resolution": "1920x1080",
  "segments": [...],
  "bgm_analysis": {
    "music_style": {"primary": "Pop"},
    "emotion": {"primary": "Cheerful"},
    "tempo": {"bpm_estimate": 120, "pace": "Medium"}
  },
  "scene_analysis": {
    "primary_scene": "Indoor",
    "video_style": {"overall": "Lifestyle", "target_audience": ["Young people"]},
    "platform_recommendations": [...]
  }
}
```

### hook_analysis.json (Optional)

```json
{
  "overall_score": 7.5,
  "visual_impact": 8.0,
  "visual_comment": "Evaluation...",
  "language_hook": 7.0,
  "language_comment": "Evaluation...",
  "emotion_trigger": 7.5,
  "emotion_comment": "Evaluation...",
  "information_density": 7.0,
  "info_comment": "Evaluation...",
  "rhythm_control": 8.0,
  "rhythm_comment": "Evaluation...",
  "hook_type": "Curiosity type",
  "strengths": ["Strength 1", "Strength 2"],
  "weaknesses": ["Weakness 1"],
  "suggestions": ["Suggestion 1", "Suggestion 2"],
  "retention_prediction": "Medium: 50-70%, because..."
}
```

## Output Format

Full report text in Markdown format, output directly to stdout.

## Example

```shell
# Full flow
python ../video-breakdown-skill/scripts/process_video.py "https://example.com/video.mp4" > breakdown.json
cat breakdown.json | python ../hook-analyzer-skill/scripts/analyze_hook_segments.py - > hooks.json
# (hooks.json needs LLM scoring to get hook_analysis.json)
python scripts/generate_report.py breakdown.json hook_analysis.json > report.md
```

## Notes

1. If hook_analysis data is missing, the hook analysis section in the report will show "No data available"
2. Segment overview shows at most the first 10 segments
3. Report automatically adds generation timestamp
4. Visual content descriptions exceeding 40 characters are automatically truncated

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Hook analysis is empty | Confirm hook_analysis.json file was passed |
| Segment table is empty | Confirm breakdown.json contains segments field |
| BGM/Scene shows N/A | Breakdown service may not have returned this data |
