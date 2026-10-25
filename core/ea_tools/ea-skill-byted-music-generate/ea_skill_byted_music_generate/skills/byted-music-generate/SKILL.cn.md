---
name: byted-music-generate
description: 使用火山引擎 Imagination API 生成音乐。支持人声歌曲、纯音乐 BGM 和歌词生成。当用户想要创作歌曲、背景音乐、配乐、写歌词，或提到"音乐生成"、"BGM"、"写歌"时触发。
license: Complete terms in LICENSE
---

# 音乐生成技能

使用[火山引擎音乐生成 API](https://www.volcengine.com/docs/84992) 生成音乐，支持人声歌曲、纯音乐 BGM 和 AI 歌词生成。

## 触发条件

1. 用户想生成歌曲（通过歌词或文本描述）
2. 用户需要背景音乐、纯音乐或配乐
3. 用户想要 AI 生成歌词
4. 用户提到"写歌"、"音乐生成"、"BGM"、"背景音乐"、"歌词"

## 环境变量

支持两种鉴权方式（网关优先）：

**方案一：API 网关（推荐）**
- `ARK_SKILL_API_BASE` — API 网关基础地址
- `ARK_SKILL_API_KEY` — API 网关鉴权密钥

**方案二：直连 AK/SK**
- `VOLCENGINE_ACCESS_KEY` — AccessKey ID
- `VOLCENGINE_SECRET_KEY` — AccessKey Secret
- 获取方式：[火山引擎控制台](https://console.volcengine.com/) → 账号（右上角）→ 密钥管理 → 新建密钥

## 使用流程

1. 判断用户意图，选择模式（`song` / `bgm` / `lyrics`）。
2. `cd` 到技能目录：`skills/byted-music-generate`。
3. 运行脚本。脚本会自动轮询 API，可能需要**数分钟**才能完成（song/bgm 通常需要 1–5 分钟）。
4. **监控执行**：如果运行环境将命令转入后台，你必须每隔 10 秒读取终端输出，检查脚本是否完成。脚本会将轮询进度输出到 stderr，完成时输出一行 JSON 到 stdout。
5. 完成后，将 JSON 中的 `audio_url` 或 `lyrics` 返回给用户。

## 三种模式

### 1. song — 人声歌曲

用户提供歌词（Lyrics）或文本描述（Prompt）来生成人声歌曲。

```shell
# 通过文本描述
python scripts/music_generate.py song --prompt "一首关于夏天海边的歌" --genre Pop --gender Female

# 通过歌词
python scripts/music_generate.py song --lyrics "[verse]\n月光洒在窗台\n回忆像水一样流淌\n[chorus]\n你是我的月光" --genre Folk --mood "Sentimental/Melancholic/Lonely"
```

**注意**：`--lyrics` 和 `--prompt` 互斥，歌词优先。如果用户没有提供歌词，可以先用 `lyrics` 模式生成歌词，再传给 `song` 模式。

### 2. bgm — 纯音乐 BGM

用自然语言描述想要的音乐。v5.0 模型不需要 Genre/Mood 参数，在 `--text` 中描述即可。

```shell
python scripts/music_generate.py bgm --text "轻松的咖啡厅氛围音乐，带有钢琴和吉他" --duration 60

# 带曲式结构片段
python scripts/music_generate.py bgm --text "史诗感的游戏配乐" --segments '[{"Name":"intro","Duration":10},{"Name":"chorus","Duration":30}]'
```

### 3. lyrics — 歌词生成

同步返回（无需轮询）。可单独使用，也可作为 `song` 模式的前置步骤。

```shell
python scripts/music_generate.py lyrics --prompt "一首关于毕业离别的歌" --genre Folk --mood "Sentimental/Melancholic/Lonely" --gender Female
```

### 手动查询任务（超时兜底）

```shell
python scripts/music_generate.py query --task-id "202601397834584670076931"
```

## 模式判断逻辑

```
用户请求
    ↓
包含"纯音乐/BGM/背景音乐/配乐"？
    ├─ 是 → bgm ģʽ
    └─ 否 → 包含"歌词/写歌词"且不要求音频？
        ├─ 是 → lyrics ģʽ
        └─ 否 → song ģʽ
            ├─ 用户提供了歌词 → --lyrics
            └─ 用户只描述了主题 → --prompt（或先生成歌词再生成歌曲）
```

## 脚本参数

### song ģʽ

| 参数              | 必填   | 说明                                   |
|-------------------|--------|----------------------------------------|
| `--lyrics`        | 二选一 | 带结构标签的歌词                        |
| `--prompt`        | 二选一 | 文本描述（中文，5-700 字）              |
| `--model-version` | 否     | `v4.0` 或 `v4.3`（默认 v4.3）          |
| `--genre`         | 否     | 音乐流派                               |
| `--mood`          | 否     | 音乐情绪                               |
| `--gender`        | 否     | `Female`（女声）/ `Male`（男声）        |
| `--timbre`        | 否     | 人声音色                               |
| `--duration`      | 否     | 时长（秒）[30-240]                      |
| `--key`           | 否     | 调式（仅 v4.3）                         |
| `--kmode`         | 否     | `Major`（大调）/ `Minor`（小调）（仅 v4.3） |
| `--tempo`         | 否     | 节奏速度（仅 v4.3）                     |
| `--instrument`    | 否     | 乐器，逗号分隔（仅 v4.3）              |
| `--genre-extra`   | 否     | 辅助流派，逗号分隔，最多 2 个（仅 v4.3） |
| `--scene`         | 否     | 场景标签，逗号分隔（仅 v4.3）           |
| `--lang`          | 否     | 语言（仅 v4.3）                         |
| `--vod-format`    | 否     | `wav` / `mp3`（仅 v4.3）               |
| `--billing`       | 否     | `prepaid`（预付费）/ `postpaid`（后付费，默认） |
| `--timeout`       | 否     | 最大等待秒数（默认 300）                |

### bgm ģʽ

| 参数                     | 必填 | 说明                                        |
|--------------------------|------|---------------------------------------------|
| `--text`                 | 是   | 自然语言描述                                |
| `--duration`             | 否   | 时长（秒）[30-120]                          |
| `--segments`             | 否   | 曲式结构片段 JSON 数组                      |
| `--version`              | 否   | 模型版本（默认 v5.0）                       |
| `--enable-input-rewrite` | 否   | 启用提示词改写                              |
| `--billing`              | 否   | `prepaid`（预付费）/ `postpaid`（后付费，默认） |
| `--timeout`              | 否   | 最大等待秒数（默认 300）                    |

### lyrics ģʽ

| 参数       | 必填 | 说明                          |
|------------|------|-------------------------------|
| `--prompt` | 是   | 歌词描述（仅中文，<500 字）   |
| `--genre`  | 否   | 音乐流派                     |
| `--mood`   | 否   | 音乐情绪                     |
| `--gender` | 否   | `Female`（女声）/ `Male`（男声） |

## 脚本返回信息

脚本输出包含以下字段的 JSON：

```json
{
    "status": "success | timeout | error",
    "mode": "song | bgm | lyrics | query",
    "task_id": "...",
    "audio_url": "https://...",
    "duration": 46.0,
    "lyrics": "...",
    "error": null
}
```

将 `audio_url` 返回给用户下载或播放。URL 有效期约 1 年，建议用户及时保存文件。

## 错误处理

- 若脚本报错 `PermissionError: Authentication not configured ...`：提示用户配置 API 网关（`ARK_SKILL_API_BASE` + `ARK_SKILL_API_KEY`）或 AK/SK（`VOLCENGINE_ACCESS_KEY` + `VOLCENGINE_SECRET_KEY`）环境变量，写入工作区环境变量文件后重试。
- 若 `status` 为 `"timeout"`：任务仍在生成中，将 `task_id` 和手动查询命令提供给用户。
- 若版权检查失败（code 50000001）：建议用户丰富描述或增加音频时长后重试。

## 参考资料

- 可用参数值（流派/情绪/音色/乐器等）：[references/parameters.md](references/parameters.md)
- [火山引擎音乐生成文档](https://www.volcengine.com/docs/84992)
- [API 签名指南](https://www.volcengine.com/docs/6369/67269)
