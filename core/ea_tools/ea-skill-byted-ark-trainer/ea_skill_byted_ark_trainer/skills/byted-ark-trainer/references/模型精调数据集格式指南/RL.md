当进行模型精调前需要准备训练和验证的数据集。本文详细规范了模型训练的数据集格式要求提供JSONL文件结构、字段说明、示例代码及辅助工具，帮助你准备符合规范的训练数据。
请参考下面的具体格式示例，每个示例后提供了样例文件。
:::warning
精调 JSONL 文件绝对路径不可包含以下特殊字符：`*`、`?` 、`[`、`]` 。
:::

<span id="ff35b24c"></span>
# 强化学习（GRPO/PPO）
<span id="7cf8d0a9"></span>
## 多模态模型（视觉理解）
视觉理解模型支持图文混排的对话数据格式，核心基于 JSONL 文件规范，在文本对话基础上扩展了图片 / 视频抽帧的多模态数据支持。以下是完整的格式示例、说明及字段定义。
<span id="b29f4847"></span>
### 格式示例
样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。
```JSON
{"messages": [{"role": "user","content": [{"type": "text","text": "\nLet's think step by step and output the final answer within \\boxed{}."},{"type": "image_url","image_url": {"url": "data:image/{image_extension};base64,{image_base64}"}}]}],"extra": {"answer": "A"},"thinking": {"type": "enabled"}}
```

<span id="c0e1f1f5"></span>
### 格式说明
```JSON
{
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "\nLet's think step by step and output the final answer within \\boxed{}."
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/{image_extension};base64,{image_base64}"
          }
        }
      ]
    }
  ],
  "extra": {
    "answer": "A"
  },
  "thinking": {"type": "enabled"}
}
```

每一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,1.0,1.2,0.7,1.3,4"></span>
|字段名 |||类型 |是否必填 |说明 |
|---|---|---|---|---|---|
|`messages` | | |list |是 |对话列表，包含多轮角色交互 |
| |`role` | |str |是 |角色类型，支持`system`/`user`/`assistant`/`tool`，其中：|\
| | | | | ||\
| | | | | |* `system`：支持图文混排|\
| | | | | |* `user`：支持图文混排|\
| | | | | |* `assistant`：仅支持文本内容|\
| | | | | |* `tool`：如希望提升模型的Function Calling能力，需选择支持Function Calling训练的模型并提供包含Function Calling格式数据的训练集用于精调，格式可参考[附6：Function Calling 样本要求](/docs/82379/1099461#7bbc7eed)|\
| | | | | |   **最后一个message的role必须是user或tool** |
| |`content` | |str/list |是 |对话内容：|\
| | | | | ||\
| | | | | |* 当role为`assistant`：字符串格式|\
| | | | | |* 当role为`system`/`user`：数组格式，支持`text`和`image_url`子类型 |
| | |`type` |str |是（ 当role为`user`时） |子内容类型，可选`text`（文本）或`image_url`（图片） |
| | |`text` |str |是（type=text 时） |文本内容，不能为空字符串 |
| | |`image_url` |dict |是（type=image_url 时） |图片信息，包含`url`字段（图片地址）。|\
| | | | | |参考：[附1：图片文件要求](/docs/82379/1099461#94dd94ae) |
| |`extra` | |dict |否 |用于携带 reward function与custom rollout所需的字段 |
|`thinking` | | |str |否|深度思考能力控制：|\
| | | | |（支持该参数的模型选填，不支持的模型禁填） ||\
| | | | | |* 枚举值：`enabled`/`disabled`/`auto`/`custom`（不同模型支持范围不同，详情请参见[附7：深度思考能力支持情况](/docs/82379/1099461#c680ed77)） |

<span id="1de1345b"></span>
## 文本生成模型
:::warning
Seed 1.6 系列模型精调，不论纯文本输入还是多模态输入，均参考[多模态模型（视觉理解）](/docs/82379/1099461#7cf8d0a9)格式样例。
:::
<span id="3a86314f"></span>
### **格式示例**
样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。
```JSON
{"messages":[{"role":"system","content":"你是一个擅长数学计算的人工智能助手。"},{"role":"user","content":"1+1=？"}],"extra":{"answer":"2"},"thinking": {"type": "disabled"}}
```

<span id="5547d818"></span>
### 格式说明
为便于展示各个字段关系，将JSONL格式文件的一条数据展开，如下：
```JSON
{
  "messages": [
    {
      "role": "system",
      "content": "你是一个擅长数学计算的人工智能助手。"
    },
    {
      "role": "user",
      "content": "1+1=？"
    }
  ],
  "extra": {
    "answer": "2"
  },
  "thinking": {
      "type": "disabled"
  }
}
```

每一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,2,0.7,1.3,4"></span>
|字段名 ||类型 |是否必填 |说明 |
|---|---|---|---|---|
|`messages` | |list |是 |对话列表，包含多轮角色交互 |
| |`role` |str |是 |角色类型，支持`system`/`user`/`assistant`/`tool` 中的一个|\
| | | | ||\
| | | | |* `system`：仅支持文本内容|\
| | | | |* `user`：仅支持文本内容|\
| | | | |* `assistant`：仅支持文本内容|\
| | | | |* `tool`：如希望提升模型的Function Calling能力，需选择支持Function Calling训练的模型并提供包含Function Calling格式数据的训练集用于精调，格式可参考[附6：Function Calling 样本要求](/docs/82379/1099461#7bbc7eed)|\
| | | | |   **最后一个message的role必须是user或tool** |
| |`content` |str |是 |对话文本内容 |
| |`extra` |dict |否 |用于携带 reward function与custom rollout所需的字段 |
|`thinking` | |str |否|深度思考能力控制：|\
| | | |（支持该参数的模型选填，不支持的模型禁填） ||\
| | | | |* 枚举值：`enabled`/`disabled`/`auto`/`custom`（不同模型支持范围不同，详情请参见[附7：深度思考能力支持情况](/docs/82379/1099461#c680ed77)） |
