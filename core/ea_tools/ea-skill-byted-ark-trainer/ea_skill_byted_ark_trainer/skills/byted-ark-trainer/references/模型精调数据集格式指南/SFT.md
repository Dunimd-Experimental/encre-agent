当进行模型精调前需要准备训练和验证的数据集。本文详细规范了模型训练的数据集格式要求提供JSONL文件结构、字段说明、示例代码及辅助工具，帮助你准备符合规范的训练数据。
请参考下面的具体格式示例，每个示例后提供了样例文件。
:::warning
精调 JSONL 文件绝对路径不可包含以下特殊字符：`*`、`?` 、`[`、`]` 。
:::
<span id="29d51cb2"></span>
# SFT精调
<span id="a881aec9"></span>
## 多模态模型（视觉理解）
视觉理解模型支持图文混排的对话数据格式，核心基于 JSONL 文件规范，在文本对话基础上扩展了图片 / 视频抽帧的多模态数据支持。以下是完整的格式示例、说明及字段定义。
<span id="3227d669"></span>
### 格式示例
样本格式：为 JSONL 文件（**JSON Lines**，轻量级文本格式，核心规则：**每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**。
```JSON
# 通过TOS传入图片的示例
{"messages":[{"role":"system","content":"请描述图片中的物体及场景"},{"role":"user","content":[{"type":"image_url","image_url":{"url":"tos://bucketname/image/desk.png"}},{"type":"text","text":"这张图片里有什么？"}]},{"role":"assistant","reasoning_content":"用户让我描述图片中的物体和场景，首先看图片内容：画面中有一张木质书桌，上面放着笔记本电脑、咖啡杯和绿植，背景是白色墙壁和窗户，光线明亮。需要准确列出物体，描述场景氛围，保持简洁明了。","content":"图片中展示了一个整洁的办公场景：一张木质书桌上放置着笔记本电脑、白色咖啡杯和一盆绿色多肉植物，背景是带有窗户的白色墙壁，自然光线充足，整体氛围清新舒适。","loss_weight":1.0}],"thinking":{"type": "enabled"}}

# 通过base64传入图片的示例
{"messages":[{"role":"user","content":[{"type":"text","text":"计算图片中物体的数量"},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,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"}},{"type":"image_url","image_url":{"url":"file:./image/books.png"}}]},{"role":"assistant","content":"图片中包含3个主要物体：1台笔记本电脑、1个咖啡杯、1盆绿植，共3个物体。"}]}

# 通过相对路径传入图片的示例
{"messages":[{"role": "user","content": [{"type": "image_url","image_url": {"url": "file:./image/doubao_logo.jpeg"}},{"type": "text","text": "What’s in this image?"}]},{"role": "assistant","content": "It is the logo of Doubao LLM model."}]}
```

<span id="e504998a"></span>
### 格式说明
将 JSONL 文件中的一条数据展开，清晰展示字段层级关系：
```JSON
{
    "messages": [
        {
            "role": "system",
            "content": "请描述图片中的物体及场景"  // system角色仅支持文本内容
        },
        {
            "role": "user",
            "content": [  // user角色支持数组格式，混排图文
                {
                    "type": "image_url",  // 内容类型：图片
                    "image_url": {
                        "url": "tos://bucketname/image/desk.png"  // 图片地址（TOS/base64/相对路径）
                    }
                },
                {
                    "type": "text",  // 内容类型：文本
                    "text": "这张图片里有什么？"
                }
            ]
        },
        {
            "role": "assistant",
            "reasoning_content": "用户让我描述图片中的物体和场景，首先看图片内容：画面中有一张木质书桌，上面放着笔记本电脑、咖啡杯和绿植，背景是白色墙壁和窗户，光线明亮。需要准确列出物体，描述场景氛围，保持简洁明了。",
            "content": "图片中展示了一个整洁的办公场景：一张木质书桌上放置着笔记本电脑、白色咖啡杯和一盆绿色多肉植物，背景是带有窗户的白色墙壁，自然光线充足，整体氛围清新舒适。",
            "loss_weight": 1.0  // 训练权重，默认1.0
        }
    ],
    "thinking": "enabled"  // 深度思考能力控制字段
}
```

**字段说明**
每行一条 JSON 格式数据，核心字段如下：

<span aceTableMode="list" aceTableWidth="1.1,1.0,1.2,1,1.3,4"></span>
|字段名 |||类型 |是否必填 |说明 |
|---|---|---|---|---|---|
|messages | | |list |是 |对话列表，包含多轮角色交互 |
| |role | |str |是 |角色类型，支持`system`/`user`/`assistant`/`tool` 中的一个|\
| | | | | ||\
| | | | | |* `system`：支持图文混排|\
| | | | | |* `user`：支持图文混排|\
| | | | | |* `assistant`：仅支持文本内容|\
| | | | | |* `tool`：如希望提升模型的Function Calling能力，需选择支持Function Calling训练的模型并提供包含Function Calling格式数据的训练集用于精调，格式可参考[附6：Function Calling 样本要求](/docs/82379/1099461#7bbc7eed) |
| |content | |str/list |是 |对话内容：|\
| | | | | ||\
| | | | | |* 当role为`assistant`：字符串格式|\
| | | | | |* 当role为`user`/`system`：数组格式，支持`text`和`image_url`子类型 |
| | |type |str |条件必选|子内容类型，可选`text`（文本）或`image_url`（图片） |\
| | | | |当role为`user`时 | |
| | |text |str |条件必选|文本内容，不能为空字符串 |\
| | | | |typeΪ`text`ʱ | |
| | |image_url |dict |条件必选|图片信息，包含`url`字段（图片地址）。|\
| | | | |type为`image_url`时 |参考：[附1：图片文件要求](/docs/82379/1099461#94dd94ae) |
| |loss_weight | |float |否 |训练权重：|\
| | | | | ||\
| | | | | |* 当role为`system`/`user`：`loss_weight`默认 0.0，不可修改|\
| | | | | |* 当role为`assistant`：`loss_weight`默认 1.0，取值范围 [0.0,1.0] |
| |reasoning_content | |str |否 |深度思考内容，**仅允许最后一个`assistant`角色携带。** |\
| | | | | ||\
| | | | | |* 如有优化多轮对话场景下深度思考能力的精调需求，请请参考[附4：多轮reasoning_content的样本文件拆分](/docs/82379/1099461#c84133a4)。|\
| | | | | |* 如多轮对话场景训练时仅最后一轮对话携带reasoning_content，模型将学习到在中间对话轮次不进行thinking，影响模型thinking效果。 |
|thinking | | |str |否 |深度思考能力控制：|\
| | | | | ||\
| | | | | |* 部分模型精调支持该字段，|\
| | | | | |* 枚举值：`enabled`/`disabled`/`auto`/`custom`（不同模型支持范围不同，详情请参见[附7：深度思考能力支持情况](/docs/82379/1099461#c680ed77)） |

<span id="ce55c6a4"></span>
## 视频生成模型
<span id="cdd6072a"></span>
### **格式示例**
样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。
```JSON
{"output": {"content": {"video_url": "tos://${bucket}/${object}"}}}
```

<span id="facc508c"></span>
### **格式说明**
为便于展示各个字段关系，将 JSONL 格式文件的一条数据展开，如下：
```JSON
{
  "output": {
    "content": {
      "video_url": "tos://${bucket}/${object}"
    }
  }
}
```

**字段说明**
每行一条JSON格式的样本数据：

<span aceTableMode="list" aceTableWidth="1.1,1.1,1.2,0.7,1.3,3.9"></span>
|字段名 |||类型 |是否必填 |说明 |
|---|---|---|---|---|---|
|`output` | | |*list* |是 |样本输出结构 |
| |`content` | |*list* |是 |输出内容容器 |
| | |`video_url` |str |是 |视频 TOS 地址，格式为`tos://${bucket}/${object}`，需与 JSONL 文件在同一桶内。|\
| | | | | |参考：[附2：视频文件要求](/docs/82379/1099461#a59f2e98) |

<span id="e8c237be"></span>
## 文本生成模型
:::warning
Seed 1.6 系列模型精调，不论纯文本输入还是多模态输入，均参考[多模态模型（视觉理解）](/docs/82379/1099461#a881aec9)格式样例。
:::
<span id="eda52bfe"></span>
### 格式示例
样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。
您也可以[下载样例文件](https://ark-cdn.tos-cn-beijing.volces.com/samples/SFT_Text_Sample.jsonl)阅读。
```JSON
{"messages":[{"role":"system","content":"请根据古诗内容，仅回复作者的名字。"},{"role":"user","content":"姑苏城外寒山寺，夜半钟声到客船"},{"role":"assistant","reasoning_content":"用户让我根据古诗内容回复作者名字，诗句是“姑苏城外寒山寺，夜半钟声到客船”。首先，我需要确定这句诗出自哪首诗。这句诗很有名，应该是张继的《枫桥夜泊》。记得以前学过，这首诗的作者是唐代诗人张继。用户要求仅回复作者名字，所以不需要其他内容。检查一下搜索到的信息，虽然这里显示没有具体资源，但根据已有的知识，确定作者是张继没错。所以直接回复名字即可，不用多余的解释。","content":"张继","loss_weight":1}]}
{"messages":[{"role": "user", "content": "什么时候需要SFT？"}, {"role": "assistant", "content": "通过 prompt engineering 无法解决或 prompt 中描述过于复杂时；对大模型输出内容有格式要求时，而模型仍有部分 case 不符合要求；期望通过 SFT 来减少 prompt 中的内容，加速线上推理的耗时。"}]}
```

<span id="42d217fb"></span>
### 格式说明
为便于展示各个字段关系，将 JSONL 格式文件的一条数据展开，如下：
```JSON
{
    "messages": [
        {
            "role": "system",
            "content": "请根据古诗内容，仅回复作者的名字。"
        },
        {
            "role": "user",
            "content": "姑苏城外寒山寺，夜半钟声到客船"
        },
        {
            "role": "assistant",
            "reasoning_content": "用户让我根据古诗内容回复作者名字，诗句是 “姑苏城外寒山寺，夜半钟声到客船”。首先，我需要确定这句诗出自哪首诗。这句诗很有名，应该是张继的《枫桥夜泊》。记得以前学过，这首诗的作者是唐代诗人张继。用户要求仅回复作者名字，所以不需要其他内容。检查一下搜索到的信息，虽然这里显示没有具体资源，但根据已有的知识，确定作者是张继没错。所以直接回复名字即可，不用多余的解释。",
            "content": "张继",
            "loss_weight": 1
        }
    ]
}
```

**字段说明**
每行一条 JSON 格式数据，核心字段如下：

<span aceTableMode="list" aceTableWidth="1.1,2,0.7,1.3,4"></span>
|字段名 ||类型 |是否必填 |说明 |
|---|---|---|---|---|
|`messages` | |list |是 |对话列表，包含多轮角色交互 |
| |`role` |str |是 |角色类型，支持`system`/`user`/`assistant`/`tool` 中的一个|\
| | | | ||\
| | | | |* `system`：仅支持文本内容|\
| | | | |* `user`：仅支持文本内容|\
| | | | |* `assistant`：仅支持文本内容|\
| | | | |* `tool`：如希望提升模型的Function Calling能力，需选择支持Function Calling训练的模型并提供包含Function Calling格式数据的训练集用于精调，格式可参考[附6：Function Calling 样本要求](/docs/82379/1099461#7bbc7eed) |
| |`content` |str |是 |对话内容：字符串格式 |
| |`loss_weight` |float |否 |训练权重：|\
| | | | ||\
| | | | |* 当role为`system`/`user`：`loss_weight`默认 0.0，不可修改|\
| | | | |* 当role为`assistant`：`loss_weight`默认 1.0，取值范围 [0.0,1.0] |
| |`reasoning_content` |str |否 |深度思考内容，**仅允许最后一个`assistant`角色携带。** |\
| | | | ||\
| | | | |* 如有优化多轮对话场景下深度思考能力的精调需求，请参考[附4：多轮reasoning_content的样本文件拆分](/docs/82379/1099461#c84133a4)。|\
| | | | |* 如多轮对话场景训练时仅最后一轮对话携带reasoning_content，模型将学习到在中间对话轮次不进行thinking，影响模型thinking效果。 |
|`thinking` | |str |否|深度思考能力控制：|\
| | | |（支持该参数的模型选填，不支持的模型禁填） ||\
| | | | |* 枚举值：`enabled`/`disabled`/`auto`/`custom`（不同模型支持范围不同，详情请参见[附7：深度思考能力支持情况](/docs/82379/1099461#c680ed77)） |

<span id="be4351fb"></span>
## 文本向量化模型
<span id="de52b4a0"></span>
### **格式示例**
样本格式：为JSONL文件（ **JSON Lines**，是一种轻量级的文本文件格式，核心规则 **每一行对应一个独立的、合法的 JSON 对象**），需确保**单个对话样本独占一行**，示例如下。您也可以[下载样例文件](https://ark-cdn.tos-cn-beijing.volces.com/samples/SFT_TextEmbedding_Sample.jsonl)阅读。
```JSON
{"query":"乐清市服装店","docs":[{"text":"标题：先跪着把钱挣了.才能站着做选择.一定要好好赚钱.人这一生.","label":0,"loss_weight":0},{"text":"标题：看来彩礼是要高了些..CCC#服装人#抖音助手#做个快乐的女孩#实体店#穿搭#实体王府大酒店","label":1,"loss_weight":0},{"text": "标题：最近很喜欢一段话：照顾好自己的健康和情绪，这场人生，你就赢了一大半，其余的其余，人生自有","label":0,"loss_weight":0}]} 
```

<span id="b47cca6d"></span>
### **格式说明**
为便于展示各个字段关系，将JSONL格式文件的一条数据展开，如下：
```JSON
{
    "query": "乐清服装店",
    "docs": [
        {
            "text": "标题：先跪着把钱挣了.才能站着做选择.一定要好好赚钱.人这一生.",
            "label": 0
        },
        {
            "text": "标题：看来彩礼是要高了些..CCC#服装人#抖音助手#做个快乐的女孩#实体店#穿搭#实体王府大酒店",
            "label": 1
        },
        {
            "text": "标题：最近很喜欢一段话：照顾好自己的健康和情绪，这场人生，你就赢了一大半，其余的其余，人生自有",
            "label": 0
        }
    ]
}
```

每行一条JSON格式的数据：

<span aceTableMode="list" aceTableWidth="1.1,2,0.7,1.3,4"></span>
|字段名 ||类型 |是否必填 |说明 |
|---|---|---|---|---|
|`query` | |*str* |是 |检索问题文本 |
|`docs` | |*list* |是 |与检索问题文本对应的正负例目标文本列表 |
| |`text` |str |是 |正负例文本内容 |
| |`label` |bool |是 |labelֵΪ1表示该样本为正例，一条数据中需要有1个正例；labelֵΪ0表示该样本为负例，一条数据中可包含0\-5个负例 |
