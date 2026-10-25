当进行模型精调前需要准备训练和验证的数据集。本文详细规范了模型训练的数据集格式要求提供JSONL文件结构、字段说明、示例代码及辅助工具，帮助你准备符合规范的训练数据。
请参考下面的具体格式示例，每个示例后提供了样例文件。
:::warning
精调 JSONL 文件绝对路径不可包含以下特殊字符：`*`、`?` 、`[`、`]` 。
:::

<span id="4f365a2a"></span>

# 直接偏好学习（DPO）

<span id="eaa330e8"></span>

## 多模态模型（视觉理解）

视觉理解模型支持图文混排的对话数据格式，核心基于 JSONL 文件规范，在文本对话基础上扩展了图片 / 视频抽帧的多模态数据支持。以下是完整的格式示例、说明及字段定义。
数据格式支持提供多个模型回复列表，通过 score 来指定对回复的偏好。训练过程将根据 score 的大小自动生成两两对比的偏序对进行训练，注意：score 相同的两个回复不会生成相应的偏序对。 <span id="8e20ec92"></span>

### 格式示例

样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。

```JSON
{"messages":[{"role":"system","content":[{"type":"text","text":"This is a system"},{"type":"image_url","image_url":{"url":"tos://bucket/1.jpg"}}]},{"role":"user","content":[{"type":"text","text":"What's your name?"},{"type":"image_url","image_url":{"url":"tos://bucket/1.jpg"}}]},{"role":"assistant","content":[{"type":"text","text":"My name is doubao."}],"loss_weight":1.0},{"role":"user","content":[{"type":"text","text":"How to learn Python?"}]},{"role":"assistant","choices":[{"content":[{"type":"text","text":"I don't know!"}],"score":0.5,"reasoning_content":"xxx","lm_loss_mask":0},{"content":[{"type":"text","text":"Check python doc yourself"}],"score":0.1,"lm_loss_mask":0},{"content":[{"type":"text","text":"It's so easy. First, you need to learn Python syntax..."}],"score":1.0,"lm_loss_mask":0},{"content":[{"type":"text","text":"It's so easy. First, you need to learn Python syntax..."}],"score":1.0,"lm_loss_mask":0}]}],"thinking": {"type": "auto"},"tools": [{"type": "function","function": {"name": "GetCurrentWeather","description": "查询当前的天气","parameters": {"type": "object","properties": {"location": {"type": "string","description": "地理位置，比如北京市"},"unit": {"type": "string","description": "温度单位","enum": ["celsius","fahrenheit"]}},"required": ["location"]}}}]}
```

<span id="84593217"></span>

### 格式说明

为便于展示各个字段关系，将JSON格式文件的一条数据展开，如下：

```JSON
{
  "messages": [
    {
      "role": "system",
      "content": [
        {
          "type": "text",
          "text": "This is a system"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "tos://bucket/1.jpg"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "What's your name?"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "tos://bucket/1.jpg"
          }
        }
      ]
    },
    {
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "text": "My name is doubao."
        }
      ]  # assistant 轮次不能携带图片
      "loss_weight": 1.0              # assistant的loss_weight默认为1.0
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "How to learn Python?"
        }
      ]
    },
    {
      "role": "assistant",            # 最后一个message的role必须为assistant
      "choices": [                    # 可以有多个response
        {
          "content": "I don't know!",
          "score": 0.5,               # 该response的得分 ，
         "reasoning_content": "xxx"，
         "tool_calls": [
            {
              "type": "function",
              "function": {
                "name": "GetCurrentWeather",
                "arguments": "{\"location\": \"北京\"}"
              }
            }
          ]
        },
        {
          "content": [
            {
              "type": "text",
              "text": "Check python doc yourself"
            }
          ],
          "score": 0.1
        },
        {
          "content": [
            {
              "type": "text",
              "text": "It's so easy. First, you need to learn Python syntax..."
            }
          ],
          "score": 1.0,
          "lm_loss_mask": 0           # 这个response是否计算 sft loss，默认为0
        },
        {
          "content": "It's so easy. First, you need to learn Python syntax...",
          "score": 1.0,
          "lm_loss_mask": 0           # 这个response是否计算 sft loss，默认为0
        }
      ]
    }
  ],
  "thinking": {
    "type": "auto"
  },
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "GetCurrentWeather",
        "description": "查询当前的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {
              "type": "string",
              "description": "地理位置，比如北京市"
            },
            "unit": {
              "type": "string",
              "description": "温度单位",
              "enum": [
                "celsius",
                "fahrenheit"
              ]
            }
          },
          "required": [
            "location"
          ]
        }
      }
    }
  ]
}
```

每行一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,1.0,1.2,0.7,1.3,4"></span>

| 字段名        | <br />        | <br />              | 类型       | 是否必填                  | 说明                                                                                                | <br /> |
| ---------- | ------------- | ------------------- | -------- | --------------------- | ------------------------------------------------------------------------------------------------- | :----- |
| `messages` | <br />        | <br />              | list     | 是                     | 对话列表，包含多轮角色交互                                                                                     | <br /> |
| <br />     | `role`        | <br />              | str      | 是                     | 角色类型，支持`system`/`user`/`assistant`/`tool`，其中：                                                     | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | <br />                                                                                            | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* `system`：支持图文混排                                                                                | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* `user`：支持图文混排                                                                                  | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* `assistant`：仅支持文本内容                                                                            | <br /> |
| <br />     | `content`     | <br />              | str/list | 是                     | *非最后一轮的对话内容*                                                                                      | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | <br />                                                                                            | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* 当role为`assistant`：字符串格式                                                                        | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* 当role为`system`/`user`：数组格式，支持`text`和`image_url`子类型                                             | <br /> |
| <br />     | <br />        | `type`              | str      | 是（ 当role为`user`时）     | 子内容类型，可选`text`（文本）或`image_url`（图片）                                                                | <br /> |
| <br />     | <br />        | `text`              | str      | 是（type=text 时）        | 文本内容，不能为空字符串                                                                                      | <br /> |
| <br />     | <br />        | `image_url`         | dict     | 是（type=image\_url 时）  | 图片信息，包含`url`字段（图片地址）。                                                                             | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | 参考：[附1：图片文件要求](/docs/82379/1099461#94dd94ae)                                                      | <br /> |
| <br />     | `choices`     | <br />              | list     | 是                     | 仅可以在最后一轮对话中填写，且list长度在2～5之间                                                                       | <br /> |
| <br />     | <br />        | `content`           | str/list | 是                     | \* 字符串格式：文本内容                                                                                     | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* 数组格式：仅支持type为text                                                                              | <br /> |
| <br />     | <br />        | `reasoning_content` | str      | 否                     | 深度思考内容，**仅允许最后一个`assistant`角色携带。**                                                                | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | <br />                                                                                            | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* 如果打开 lm\_loss\_mask，则需要注意将非最后一轮的 assistant 对话 loss\_weight 置为 0，否则将影响模型的深度思考能力                 | <br /> |
| <br />     | <br />        | `score`             | float    | 是                     | 偏好值，范围在0～1的浮点数，越高表示越偏好                                                                            | <br /> |
| <br />     | <br />        | `lm_loss_mask`      | float    | 是                     | 该条消息是否计算 sft loss，默认为 0，即不计算 sft loss； 建议将score最大的回复lm\_loss\_mask 设为1，如果全为0 可能导致模型训练不稳定，泛化能力变差。  | <br /> |
| <br />     | `loss_weight` | <br />              | float    | 否                     | 该条 message loss 的加权系数，默认为 1                                                                       | <br /> |
| `thinking` | <br />        | <br />              | str      | 否                     | 深度思考能力控制：                                                                                         | \\     |
| <br />     | <br />        | <br />              | <br />   | （支持该参数的模型选填，不支持的模型禁填） | <br />                                                                                            | \\     |
| <br />     | <br />        | <br />              | <br />   | <br />                | \* 枚举值：`enabled`/`disabled`/`auto`（不同模型支持范围不同，详情请参见[附7：深度思考能力支持情况](/docs/82379/1099461#c680ed77)） | <br /> |

<span id="be96139b"></span>

## 文本生成模型

:::warning
Seed 1.6 系列模型精调，不论纯文本输入还是多模态输入，均参考[多模态模型（视觉理解）](/docs/82379/1099461#eaa330e8)格式样例。
::: <span id="af07b118"></span>

### 标准格式

<span id="6dcd9e1b"></span>

#### **格式示例**

样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。支持两个回答正负例的偏好对比学习，您也可以[下载样例文件](https://ark-cdn.tos-cn-beijing.volces.com/samples/DPO_Text_Sample.jsonl)阅读。

```JSON
{"messages":[{"role":"system","content":"This is a system"},{"role":"user","content":"What your name?"},{"role":"assistant","content":"My name is doubao."},{"role":"user","content":"How to learn Python?"},{"role":"assistant","chosen":"It's so easy. First, you need to learn Python syntax...","rejected":"Check python doc yourself"}]}
```

<span id="afa8d0ce"></span>

#### 格式说明

为便于展示各个字段关系，将JSONL格式文件的一条数据展开，如下：

```JSON
{
    "messages": [
        {
            "role": "system",
            "content": "This is a system"
        },
        {
            "role": "user",
            "content": "What is your name?"
        },
        {
            "role": "assistant",
            "content": "My name is doubao."
        },
        {
            "role": "user",
            "content": "How to learn Python?"
        },
        {
            "role": "assistant",
            "chosen": "It's so easy. First, you need to learn Python syntax...",
            "rejected": "Check python doc yourself"
        }
    ]
}
```

每行一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,2,0.7,1.3,4"></span>

| 字段名        | <br />     | 类型     | 是否必填   | 说明                                 | <br /> |
| ---------- | ---------- | ------ | ------ | ---------------------------------- | :----- |
| `messages` | <br />     | list   | 是      | 对话列表，包含多轮角色交互                      | <br /> |
| <br />     | `role`     | str    | 是      | 角色类型，支持`system`/`user`/`assistant` | \\     |
| <br />     | <br />     | <br /> | <br /> | **最后一个message的role必须是assistant**   | <br /> |
| <br />     | `content`  | str    | 是      | 对话内容文本，最后一个message不包含              | <br /> |
| <br />     | `chosen`   | str    | 是      | 偏好的内容正例，最后一个message必须包含            | <br /> |
| <br />     | `rejected` | str    | 是      | 不偏好的内容负例，最后一个message必须包含           | <br /> |

<span id="dc98f9eb"></span>

### **高级格式**

高级格式支持提供多个模型回复列表，通过 score 来指定对回复的偏好。训练过程将根据 score 的大小自动生成两两对比的偏序对进行训练，注意：score 相同的两个回复不会生成相应的偏序对。 <span id="dda33174"></span>

#### **格式示例**

需确保**单个对话样本独占一行**，示例如下。您也可以[下载样例文件](https://ark-project.tos-cn-beijing.volces.com/jsonl/dpo.yaml)阅读。

```JSON
{"messages":[{"role":"system","content":[{"text":"This is a system"}]},{"role":"user","content":[{"text":"What your name?"}]},{"role":"assistant","content":[{"text":"My name is doubao."}]},{"role":"user","content":[{"text":"How to learn Python?"}]},{"role":"assistant","content":[{"text":"I don't know!","score":0.5},{"text":"Check python doc yourself","score":0.1},{"text":"It's so easy. First, you need to learn Python syntax...","score":1,"lm_loss_mask":1}],"loss_weight":1}]}
```

<span id="c7a119da"></span>

#### 格式说明

为便于展示各个字段关系，将文件的一条数据展开，如下：

```JSON
{
    "messages": [
        {
            "role": "system",
            "content": [
                {
                    "text": "This is a system"
                }
            ]
        },
        {
            "role": "user",
            "content": [
                {
                    "text": "What is your name?"
                }
            ]
        },
        {
            "role": "assistant",
            "content": [
                {
                    "text": "My name is doubao."
                }
            ]
        },
        {
            "role": "user",
            "content": [
                {
                    "text": "How to learn Python?"
                }
            ]
        },
        {
            "role": "assistant",
            "content": [
                {
                    "text": "I don't know!",
                    "score": 0.5
                },
                {
                    "text": "Check python doc yourself",
                    "score": 0.1
                },
                {
                    "text": "It's so easy. First, you need to learn Python syntax...",
                    "score": 1,
                    "lm_loss_mask": 1
                }
            ],
            "loss_weight": 1
        }
    ]
}
```

每行一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,1.0,1.2,0.7,1.3,4"></span>

| 字段名        | <br />        | <br />         | 类型       | 是否必填   | 说明                                                    | <br /> |
| ---------- | ------------- | -------------- | -------- | ------ | ----------------------------------------------------- | :----- |
| `messages` | <br />        | <br />         | list     | 是      | 对话列表，包含多轮角色交互                                         | <br /> |
| <br />     | `role`        | <br />         | str      | 是      | 角色类型，支持`system`/`user`/`assistant`，其中：                | \\     |
| <br />     | <br />        | <br />         | <br />   | <br /> | **最后一个message的role必须是assistant，且仅最后一轮对话可以携带多个回复的偏序对** | <br /> |
| <br />     | `content`     | <br />         | str/list | 是      | **最后一个message的content必须为list类型，且list长度在2～5之间**        | <br /> |
| <br />     | <br />        | `text`         | str      | 是      | 文本内容                                                  | <br /> |
| <br />     | <br />        | `score`        | float    | 是      | 偏好值，范围在0～1的浮点数，越高表示越偏好                                | <br /> |
| <br />     | <br />        | `lm_loss_mask` | float    | 是      | 该条消息是否计算 sft loss，默认为 0，即不计算 sft loss                 | <br /> |
| <br />     | `loss_weight` | <br />         | float    | 否      | 该条 message loss 的加权系数，默认为 1                           | <br /> |

