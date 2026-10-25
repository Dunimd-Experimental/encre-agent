---
name: hook-analyzer-skill
description: Video first-three-seconds hook analysis skill. Extracts first-three-seconds segment data from breakdown results to provide structured context for hook appeal evaluation. Run the script `python scripts/analyze_hook_segments.py "<breakdown_json_file>"` or pass JSON via stdin. Output includes first-three-seconds segment count, total duration, and detailed info for each segment (including keyframe URLs).
---

# Video First-Three-Seconds Hook Analysis

## Overview

The video first-three-seconds hook analysis skill extracts segment data from the first three seconds of the breakdown results and constructs a structured analysis context, including visual content description, keyframe image URLs, camera movement information, etc., providing input for subsequent LLM multi-dimensional scoring analysis.

## Use Cases

1. **Short video optimization**: Evaluate the appeal of the video opening to improve 3-second retention rate
2. **Creative review**: Quantitatively analyze hook quality across 5 dimensions
3. **Competitive benchmarking**: Compare hook strategies across different video openings

## Analysis Dimensions

The data extracted by this skill is used for scoring across the following 5 dimensions (scored by LLM):

| Dimension | Weight | Evaluation Points |
|-----------|--------|-------------------|
| Visual Impact | 30% | Composition, color, lighting, camera dynamics |
| Language Hook | 25% | Copy-to-visual alignment, suspense creation |
| Emotional Arousal | 15% | Scene emotion, facial expressions, atmosphere |
| Information Density | 15% | Effective information volume, core value delivery |
| Rhythm Control | 15% | Segment transition rhythm, platform adaptation |

## Usage Steps

### Method 1: Read segment data from file

```shell
# 1. First use video-breakdown skill to process the video and get segment results
python ../video-breakdown-skill/scripts/process_video.py "https://example.com/video.mp4" > breakdown.json

# 2. Extract first-three-seconds segment data
python scripts/analyze_hook_segments.py breakdown.json
```

### Method 2: Pipe via stdin

```shell
cat breakdown.json | python scripts/analyze_hook_segments.py -
```

## Output Format

```json
{
  "segment_count": 3,
  "total_duration": 2.8,
  "total_video_segments": 15,
  "analysis_mode": "multimodal",
  "segments": [
    {
      "index": 0,
      "start_time": 0.0,
      "end_time": 1.0,
      "duration": 1.0,
      "visual_content": "Visual description",
      "speech_text": "Spoken text",
      "shot_type": "Close-up",
      "camera_movement": "Push in",
      "function_tag": "Opening",
      "frame_images": [
        {"type": "image_url", "image_url": {"url": "https://..."}}
      ],
      "frame_count": 3
    }
  ]
}
```

## Hook Type Classification

The analysis results can be used to identify the following hook types:

- **Pain point type**: Directly hits user pain points, triggers resonance
- **Curiosity type**: Creates suspense, triggers curiosity
- **Conflict type**: Creates contrast or conflict to grab attention
- **Value type**: Directly displays value proposition
- **Emotional type**: Moves users through emotional resonance
- **Visual impact type**: Grabs attention through strong visual effects
- **Suspense type**: Leaves suspense to guide continued viewing

## Scoring Criteria

| Score Range | Level | Description |
|-------------|-------|-------------|
| 9-10 | Top | Extremely strong appeal and creativity |
| 7-8 | Excellent | Good appeal |
| 5-6 | Average | Room for improvement |
| 3-4 | Weak | Needs major improvement |
| 1-2 | Very poor | Basically no appeal |

## Notes

1. This script only does **data extraction**, not LLM scoring (scoring is done by the Agent's LLM)
2. The extracted `frame_images` field contains keyframe URLs that can be directly analyzed by Vision models
3. Each segment takes at most the first 3 keyframes to avoid token limits
4. Input must be the complete JSON data returned by `process_video`

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Empty output segments | Confirm input JSON contains `segments` field and it is not empty |
| No segments in first three seconds | Video may start with a static scene, check original data |
| Keyframe URL expired | TOS signed URL may have expired, re-process to get fresh frames |

