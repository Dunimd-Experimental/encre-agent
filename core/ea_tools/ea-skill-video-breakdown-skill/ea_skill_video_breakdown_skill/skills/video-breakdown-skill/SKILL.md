---
name: video-breakdown-skill
description: Video breakdown skill (self-contained, no external backend needed). Uses FFmpeg to preprocess video and generate segment data. (1) Process video URL directly `python scripts/process_video.py "<video_url>"`; (2) For local files, upload first `python scripts/video_upload.py "<file_path>"` to get a URL, then process. Requires FFmpeg installed locally.
---

# Video Breakdown

## Overview

The video breakdown skill uses local FFmpeg to automatically decompose a video into frame-by-frame segments, providing keyframe images and timing information for each shot. No external backend service required.

## Prerequisites

- FFmpeg installed locally: `brew install ffmpeg`

## Usage Steps

### Method 1: Process Video URL Directly

```shell
python scripts/process_video.py "https://example.com/video.mp4"
```

### Method 2: Upload Local File Then Process

```shell
# 1. Upload local video to TOS
python scripts/video_upload.py "/path/to/video.mp4"

# 2. Process using the returned video_url
python scripts/process_video.py "<video_url>"
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FFMPEG_BIN` | No | FFmpeg path, default `ffmpeg` |
| `FFPROBE_BIN` | No | FFprobe path, default `ffprobe` |
| `VOLCENGINE_ACCESS_KEY` | Required for upload | Volcano Engine Access Key |
| `VOLCENGINE_SECRET_KEY` | Required for upload | Volcano Engine Secret Key |
| `DATABASE_TOS_BUCKET` | No | TOS bucket name |
| `DATABASE_TOS_REGION` | No | TOS region, default `cn-beijing` |

## Output Format

```json
{
  "task_id": "xxx",
  "duration": 30.5,
  "resolution": "1920x1080",
  "segment_count": 12,
  "segments": [
    {
      "index": 1,
      "start": 0.0,
      "end": 3.0,
      "frame_paths": ["path/to/frame.jpg"]
    }
  ]
}
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| FFmpeg not found | Install FFmpeg: `brew install ffmpeg` |
| Upload failed | Check AK/SK configuration and TOS bucket |
| Video download failed | Confirm URL is valid and publicly accessible |
