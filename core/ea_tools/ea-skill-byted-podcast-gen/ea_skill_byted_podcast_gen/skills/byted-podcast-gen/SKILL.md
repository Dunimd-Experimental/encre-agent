---
name: byted-podcast-gen
description: Synthesize summaries of topics or web page content into podcast audio. Generates final audio based on Volcengine Doubao voice podcast synthesis protocol.
---
# Podcast Skill
Synthesizes topics into podcast audio and saves as local files, based on the Volcengine Doubao Voice Synthesis WebSocket Protocol (PodcastTTS, `/api/v3/sami/podcasttts`). Supports:
- Input a topic sentence or a web page URL (or a file download URL, supporting pdf/word/txt formats) to generate a podcast
- Output the podcast audio download link as-is (do not truncate or modify) and the generated local file for download. Verify whether the download link is accessible; if so, return it to the user; if not, return only the local file.
- Output podcast segmented text (JSON)

## Applicable Scenarios
1. The user mentions keywords such as `generate podcast` or `podcast synthesis`.
2. The user needs to generate a podcast audio file for a topic.
3. The user needs to generate a podcast audio file from a web page or file content.
4. The user needs to generate a podcast audio file from uploaded file content or a long context.

## Mandatory Rules (Highest Priority)

**When you receive a user request to generate a podcast:**
- **You must and may only use** this Skill's scripts to generate the podcast
- **Topic Mode**: When the user needs to generate a podcast audio file for a topic, use parameters `action=4` and `prompt_text` = topic text.
- **Webpage Mode**: When the user needs to generate a podcast audio file from a webpage or downloadable file content, use parameters `action=0` and `input_url` = webpage address or file download URL.
- **File Mode**: When the user needs to generate a podcast audio file from uploaded file content or a long context, use parameters `action=0` and `text` = content read from the user's uploaded file or a relatively long text (generally over 200 characters).

## Usage Steps
1. Analyze the content the user wants to synthesize into a podcast, and prepare the input: `prompt_text` (original topic, generally no more than 20 characters) or `input_url` (web page URL or file download URL) or `text` (content read from the user's uploaded file or a relatively long text, generally over 200 characters).
2. Before running the script, `cd` to this skill's directory: `skills/byted-podcast-gen`.
3. Configure authentication (environment variables or command-line parameters).
4. Execute the script: `python scripts/podcast.py [parameters]`. Refer to the examples section below.
5. Use the result from the script's JSON output via `audio_path` / `texts` / `audio_url`. If `audio_url` is a URL with an expiration time, return it to the user as-is. `audio_path` is the local file path and can be provided to the user for download.

## Script Parameters
| Parameter | Short | Required | Description |
|------|------|------|------|
| `--text` | | No | Input raw long text (used with `action=0`) |
| `--input_url` | | No | URL of the input text (used with `action=0`, alternative to --text) |
| `--prompt_text` | | No | Prompt text (required with `action=4`) |
| `--action` | | No | Podcast type: `0` (raw text/URL), `4` (prompt); default `4` |
| `--speaker_info` | | No | Speaker configuration JSON (default `{"random_order":false}`) |
| `--encoding` | | No | Audio format: `mp3` (default), `wav`, `ogg_opus` |
| `--output` | | No | Final audio output file path (default auto-generated to `output/`) |

## Return Value Description
The script outputs JSON containing:
- `status`: `"success"` or `"error"`
- `task_id`: Task identifier (for locating a specific generation task)
- `audio_path`: Local path of the final audio
- `texts`: Segmented text JSON string, a list of text for each speaker.
- `audio_url`: Audio download URL returned by the server
- `error`: Error message on failure

## Error Handling
- If the error indicates missing `MODEL_SPEECH_API_KEY`: Check whether the environment variable or command-line parameter is configured. If not present, prompt the user to input it, then set it as an environment variable.
- If a server error is received (`MsgType.Error`): Check account permissions, resource ID, input content, and whether the service has been activated based on the error message.
- If the server error contains the keyword `quota`, the current account has exceeded its limit and needs to upgrade the Volcengine Doubao voice podcast service.
- If a Python execution error indicates a missing package, first install the dependencies: `pip install -r requirements.txt`

## Reference Documentation
- [Doubao Podcast - Product Introduction](https://www.volcengine.com/docs/6561/1668014?lang=zh)

## Examples
```shell
# Generate podcast audio based on a topic
ptompt_text="豆包语音合成服务 (Doubao voice synthesis service)"
python scripts/podcast.py --prompt_text $ptompt_text --action 4
# Generate podcast audio based on web page content
url="https://www.volcengine.com/docs/6561/1668014?lang=zh"
python scripts/podcast.py --input_url $url --action 0
# Generate podcast audio based on long text content
text="欢迎收听本期节目，我们聊聊人工智能的关键拐点…… (Welcome to this episode, let's talk about the key turning points of AI...)"
python scripts/podcast.py --text $text --action 0
```
