---
name: byted-mediakit-editing
description: "Audio/video editing, covering audio/video concatenation, trimming, compositing, etc. Capabilities include: add-image-to-video, add-subtitle-to-video, adjust-audio-speed, adjust-video-speed, adjust-video-volume, apply-video-filter, concat-audio, concat-video, extract-audio, fade-audio, fade-video-audio, flip-video, image-to-video, mix-audio, mux-audio-video, trim-audio, trim-video. Triggered when users need to use MediaKit CLI capabilities in the editing domain."
    bins: ["mediakit-cli"]
  cliHelp: "mediakit-cli editing --help"
  product: mediakit-cli/skills
  domain: editing
  capability_count: 17
---
# Editing Skills

## Prerequisites

Before starting, you must read the contents of `./reference/shared.md`, which contains instructions on prerequisite checks, async task mechanisms, result queries, etc.

## Tool List

| Tool | Description | Parameter Declaration | Reference |
|------|------|----------|----------|
| add-image-to-video | Add an image to a video, can be used as an image watermark. | `video_url:string, sub_image_url:string, sub_image_height?:string, sub_image_width?:string, sub_image_pos_x?:string, sub_image_pos_y?:string, start_time?:number, end_time?:number, callback_args?:string, client_token?:string` | [reference/add-image-to-video.md](reference/add-image-to-video.md) |
| add-subtitle-to-video | Burn subtitle files or text content onto video frames with specified styles, generating a new video with embedded subtitles. | `video_url:string, subtitle_url?:string, subtitles?:array<object{subtitle_text:string, start_time:number, end_time:number}>, subtitle_pos_preset?:string, subtitle_font_size?:integer, subtitle_font_color?:string, subtitle_font_type?:string, callback_args?:string, client_token?:string` | [reference/add-subtitle-to-video.md](reference/add-subtitle-to-video.md) |
| adjust-audio-speed | Adjust the playback speed of audio, achieving fast-forward or slow-motion effects. | `audio_url:string, speed?:number, callback_args?:string, client_token?:string` | [reference/adjust-audio-speed.md](reference/adjust-audio-speed.md) |
| adjust-video-speed | Adjust the playback speed of video, achieving fast-forward or slow-motion effects. | `video_url:string, speed?:number, callback_args?:string, client_token?:string` | [reference/adjust-video-speed.md](reference/adjust-video-speed.md) |
| adjust-video-volume | Adjust video volume, supports muting; outputs mp4 with resolution matching the source. | `video_url:string, volume?:number, callback_args?:string, client_token?:string` | [reference/adjust-video-volume.md](reference/adjust-video-volume.md) |
| apply-video-filter | Apply specified filter effects to video, outputs mp4 with resolution matching the source. | `video_url:string, filter_style?:string, callback_args?:string, client_token?:string` | [reference/apply-video-filter.md](reference/apply-video-filter.md) |
| concat-audio | Concatenate multiple audio segments. | `audio_urls:array<string>, callback_args?:string, client_token?:string` | [reference/concat-audio.md](reference/concat-audio.md) |
| concat-video | Concatenate multiple video segments, supports adding transition effects. | `video_urls:array<string>, transitions?:array<string>, callback_args?:string, client_token?:string` | [reference/concat-video.md](reference/concat-video.md) |
| extract-audio | Extract the audio stream from a video file and save it as an independent audio file. | `video_url:string, format?:string, callback_args?:string, client_token?:string` | [reference/extract-audio.md](reference/extract-audio.md) |
| fade-audio | Apply fade-in and fade-out effects to input audio, outputs mp3. | `audio_url:string, fade_in_duration?:number, fade_out_duration?:number, callback_args?:string, client_token?:string` | [reference/fade-audio.md](reference/fade-audio.md) |
| fade-video-audio | Apply fade-in and fade-out effects to the audio track of input video. Outputs mp4 with resolution matching the source. | `video_url:string, fade_in_duration?:number, fade_out_duration?:number, callback_args?:string, client_token?:string` | [reference/fade-video-audio.md](reference/fade-video-audio.md) |
| flip-video | Flip the video frame vertically or horizontally (mirror effect). | `video_url:string, is_flip_vertical?:boolean, is_flip_horizontal?:boolean, callback_args?:string, client_token?:string` | [reference/flip-video.md](reference/flip-video.md) |
| image-to-video | Generate an animated video from multiple images. | `images:array<object{image_url:string, duration?:number, animation_type?:string, animation_in?:number, animation_out?:number}>, transitions?:array<string>, callback_args?:string, client_token?:string` | [reference/image-to-video.md](reference/image-to-video.md) |
| mix-audio | Mix multiple audio files (e.g., background music, sound effects, vocals) into a new audio file. Processing time is positively correlated with video duration. Average RTF (processing time/source duration) is 1. Output audio duration is based on the longest audio. Output format: mp3 | `audio_urls:array<string>, callback_args?:string, client_token?:string` | [reference/mix-audio.md](reference/mix-audio.md) |
| mux-audio-video | Mux audio and video streams together. | `video_url:string, audio_url:string, is_audio_reserve?:boolean, is_video_audio_sync?:boolean, sync_mode?:string, sync_method?:string, callback_args?:string, client_token?:string` | [reference/mux-audio-video.md](reference/mux-audio-video.md) |
| trim-audio | Trim audio by start and end time points (in seconds), generating a new segment. | `audio_url:string, start_time?:number, end_time?:number, callback_args?:string, client_token?:string` | [reference/trim-audio.md](reference/trim-audio.md) |
| trim-video | Trim video by start and end time points, generating a new segment. | `video_url:string, start_time?:number, end_time?:number, callback_args?:string, client_token?:string` | [reference/trim-video.md](reference/trim-video.md) |
