# 强化学习配置
## 强化学习流程
创建强化学习任务时，需配置强化学习流程custom_rl_pipeline，先选定 pipeline 类型，再按要求配置对应 plugin 函数。具体要求如下：

**支持的 pipeline 类型：**
- GRPOPipeline 和 PPOPipeline

**rollout plugin 要求：**
- 仅支持填入 1 个自定义 rollout plugin，不填默认使用单轮模型推理 rollout 逻辑

**grader plugin 要求：**
- 至少需要填入一个 grader plugin，支持填写多个并分别配置权重

## 强化学习插件（plugin）
目前支持以下自定义plugin类型：
- Rollout plugin函数，通过@rollout 装饰器标记，支持在函数内实现自定义Rollout逻辑（如某个特定业务场景的多轮推理、多次工具调用的agent）
- Grader plugin函数，用于计算reward奖励分数，目前支持：
  - 单样本评分器（single_grader），通过@single_grader 装饰器标记。针对一条独立采样打分。平台将同一条样本的n_generation 拆解成多个请求分别独立打分。
  - 多样本评分器 (group_grader)，通过@group_grader装饰器标记。针对一组样本（不限定特定条数，一般为多条）打分，一次调用输入n_samples，返回一组得分

可在工作仓库任意位置实现满足规范的函数，并使用 SDK 提供的装饰器来为函数声明 plugin 相关的元信息与运行时配置。如果未填写装饰器信息，函数不能作为 plugin 函数使用。

### 装饰器
用于标记 plugin 函数类型，声明 plugin 相关元信息与运行时配置。提交任务时将根据plugin类型校验是否满足函数签名和强化学习pipeline是否完备。

**类型：** 支持@rollout/@single_grader/ @group_grader三种类型的装饰器

**参数：**
- name：可选，为 plugin 指定名字，不提供时默认使用函数名
- description：可选，为 plugin 附加描述
- runtime.instance：可选，指定 plugin 运行实例规格，当过载时可适当增加。默认值 CPU1MEM2（对应cpu1核内存2gb）、最大值CPU16MEM128，cpu核数：内存gb数=1:2、1:4、1:8
- runtime.timeout：可选，plugin 函数执行的超时秒数，默认值取决于服务端逻辑，最大值900。plugin耗时过高会导致训练资源闲置增加费用，建议尽可能优化减少耗时
- runtime.max_concurrency：可选，单 plugin 函数实例最大请求并发数，超该值触发扩容，可根据负载情况调整。默认值10，最大100，最小1

我们提供了以下Rollout plugin函数模版和Grader plugin函数模版，您可参考签名要求和函数模版实现所需函数。

### Rollout函数
Rollout 函数主要实现 agent loop 的逻辑，函数提供 OpenAI 兼容的模型 API，用户完成 sample 过程。这个需用通过 @rollout这个装饰器来指定一些函数运行信息。

**Rollout签名要求：**
```python
class RolloutResult(BaseModel):
    status: str # success/failure/discard/retry
    error: str
    extra: Dict[str, Any]


async def demo_rollout(
    context: PluginContext,
    proxy: RolloutInferenceProxy,
    sample: ChatCompletionSample,
) -> Optional[RolloutResult]:
```

**入参：**

| 字段 | 类型 | 描述 |
|------|------|------|
| context | PluginContext | 该请求对应的任务信息，包含任务 Id、模型名、模型版本、训练方式、phase: 样本来自什么阶段，train/test(验证集） |
| proxy | RolloutInferenceProxy | 通过这个对象可以获得 client 请求模型 |
| sample | ChatCompletionSample | 输入样本  |

**context 示例：**
```json
{
  "modle_customization_job_id": "mcj_xxxxxx_xxx",
  "foundation_model_name": "doubao-1-5-lite-32k",
  "foundation_model_version": "250115",
  "customization_type": "GRPO",
  "phase":"train",
  "is_mock": false
}
```

**proxy 使用示例：**
```python
# Async client
client = proxy.async_rollout_client()
completion = await client.chat.completions.create(
    # model 字段仅在本地测试时生效
    model=LOCAL_TEST_MODEL,
    messages=messages,
    tools=tools,
)

# Sync client
client = proxy.rollout_client()
completion = client.chat.completions.create(
    # model 字段仅在本地测试时生效
    model=LOCAL_TEST_MODEL,
    messages=messages,
    tools=tools,
)
```

**sample 示例：**
```python
class ChatCompletionSample:
    # GenerationConfig
    n: int = 1
    max_new_tokens: int = 4096
    top_p: float = 1.0
    top_k: int = 0
    temperature: float = 1.0

    messages: List[ChatCompletionMessage]
    tools: Optional[List[ChatCompletionToolParam]] = None
    # ark
    thinking: Optional[Thinking] = None
    extra: Optional[Dict[str, Any]] = None


class ChatCompletionMessage(BaseModel):
    role: str
    content: Any
    tool_calls: Optional[List[ChatCompletionMessageToolCall]] = None
    reasoning_content: Optional[str] = ""


class ChatCompletionMessageToolCall(BaseModel):
    id: Optional[str] = "user_defined"

    function: Function

    type: Literal["function"]
```

```json
{
  "messages": [
    {
      "role": "user",
      "content": "北京天气怎么样"
    }
  ],
  "tools": [
    {
      "function": {
        "name": "get_current_weather",
        "description": "获取指定地点的天气信息",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {
              "type": "string",
              "description": "地点的位置信息，例如北京、上海"
            },
            "unit": {
              "type": "string",
              "enum": [
                "摄氏度",
                "华氏度"
              ],
              "description": "温度单位"
            }
          },
          "required": [
            "location"
          ]
        }
      },
      "type": "function"
    }
  ],
  "model": "doubao-seed-1-6-250615"
}
```

**返回：**

| 参数 | 类型 | 描述 |
|------|------|------|
| status | str | success/failure: 引擎重试三次仍然失败则任务失败/discard: 直接丢弃该样本/retry：引擎重试三次仍失败则丢弃该样本 |
| error | str | 如果status 为 failure，可携带具体失败原因或错误栈信息，会打印到训练日志中 |
| extra | Dict[str, Any] | 可以添加任意数据，并且会透传到自定义 reward |

**Rollout函数模版**

**ChatAPI+自定义工具**
基于精调 SDK 封装的 Chat API，搭配自定义工具实现业务逻辑，适合快速开发常规工具调用类场景（如天气查询、信息检索）。可参考rl_demo中weather_rollout.py模版。

若需更底层的控制能力，可参考rl_demo中raw_rollout.py模版，手动调用proxy.update_state_from_messages和proxy.process_completion处理训练状态，实现同步化的底层逻辑控制。

**Arctict框架+多轮推理+MCP**
基于 Arkitect 框架与 MCP（Multi-Client Proxy）多客户端代理机制，适合融合多工具、复杂多轮推理的场景（如深度搜索、多步骤任务处理）。可参考rl_search_mcp_demo中draft_rollout_arkitect.py模版。

**Arctict框架+functioncall+API调用插件**
该方案结合 Arkitect 框架的 Function Call 能力与第三方 API 插件（如搜索 API），适合需要自定义 API 调用、严格参数校验的场景（如定制化搜索、第三方服务集成）。可参考rl_search_mcp_demo中rolllout.py模版。

### Grader函数

Grader 函数用于计算reward奖励分数，目前支持通过@single_grader装饰器标记的单样本评分器和通过@group_grader装饰器标记的多样本评分器。

**Grader签名要求**

```python
@dataclass
class RewardFunctionResult:
    rewards: List[float]
    metrics: Dict[str, float]
    status: str # success/failure/discard/retry
    error: str

def reward_fn(
    context: Dict[str, Any],
    sample: Dict[str, Any],
    trajectories: list[Dict[str,Any]])-> RewardFunctionResult
    #@group grader装饰器标记的多样本评分器为trajectories，此处以trajectories为例；
    #@Single grader装饰器标记的单样本评分器为trajectory，见下方入参详细说明。
```

**入参**

| 字段 | 类型 | 描述 |
|------|------|------|
| context | Dict[str, Any] | 该请求对应的任务信息，包含任务 Id、模型名、模型版本、训练方式、phase: 样本来自什么阶段，train/test(验证集） |
| sample | Dict[str, Any] | rollout 输入的样本，与数据集内容完全一致。注意：纯文本模型，content字段仅支持str；1.6 模型或其他多模态模型，content 字段会转换为 list |
| trajectories | list[Dict[str,Any]] | 通过@group_grader装饰器标记的多样本评分器。一条样本的所有 rollout 输出的结果。假设一次 rollout 采样 n 次，trajectory 长度就为 n。messages 类型为数组，非 agent rl 场景长度固定为 1+len(sample)。注意：纯文本模型，content字段仅支持str；1.6 模型或其他多模态模型，content 字段会转换为 list |
| trajectory | Dict[str,Any] | 通过@single_grader装饰器标记的单样本评分器。一条样本的一个 rollout 输出的结果。messages 类型为数组，非 agent rl 场景长度固定为 1 |

**入参样例**

**context 样例：**
```json
{
  "modle_customization_job_id": "mcj_xxxxxx_xxx",
  "foundation_model_name": "doubao-1-5-lite-32k",
  "foundation_model_version": "250115",
  "customization_type": "GRPO",
  "phase":"train",
  "is_mock": false,
}
```

**sample 样例：**

**多模态模型样例**
```json
{
  "messages": [
    {
      "role": "system",
      "content": "你是一个擅长数据计算的人工智能助手。"
    },
    {
      "role": "user",
      "content": {
            "type": "text",
            "text": "1+1=？"
                 }
    }
  ],
  "tools": [],
  "extra": {
    "answer": 1234
  }
}
```

**纯文本模型样例**
```json
{
  "messages":
    {
      "role": "user",
      "content": "1+1=？"
    }
}
```

**trajectories 样例：**
```json
[
  {
    "role": "system",
    "content": "你是一个擅长数据计算的人工智能助手。"
  },
  {
    "role": "user",
    "content": "1+1=？"
  },
  {
    "messages": [
      {
        "content": "等于 2",
        "role": "assistant"
      }
    ],
    "finish_reason": "stop",
    "usage": {
      "completion_tokens": 3,
      "prompt_tokens:": 20,
      "total_tokens": 23
    }
  },
  {
    "messages": [
      {
        "reasoning_content": "嗯，用户问的是 1 加 1 等于多少。首先，我需要确认这是一个基本的算术问题。在常规的十进制数学中，1 加 1 的结果是 2。这是最基础的加法运算，应该没有其他复杂的情况需要考虑。用户可能是在测试我的基本计算能力，或者是刚开始学习数学的小朋友。所以直接回答 2 就可以了。",
        "content": "1 + 1 等于 2。这是基础的算术加法运算，在十进制计数系统中，1 和 1 相加的结果是 2。",
        "role": "assistant"
      }
    ],
    "finish_reason": "stop",
    "usage": {
      "completion_tokens": 109,
      "prompt_tokens:": 20,
      "total_tokens": 129
    }
  }
]
```

**trajectory 样例：**
```json
{
  "role": "system",
  "content": "你是一个擅长数据计算的人工智能助手。"
},
{
  "role": "user",
  "content": "1+1=？"
},
{
"messages": [
  {
    "reasoning_content": "嗯，用户问的是 1 加 1 等于多少。首先，我需要确认这是一个基本的算术问题。在常规的十进制数学中，1 加 1 的结果是 2。这是最基础的加法运算，应该没有其他复杂的情况需要考虑。用户可能是在测试我的基本计算能力，或者是刚开始学习数学的小朋友。所以直接回答 2 就可以了。",
    "content": "1 + 1 等于 2。这是基础的算术加法运算，在十进制计数系统中，1 和 1 相加的结果是 2。",
    "role": "assistant"
  }
],
"finish_reason": "stop",
"usage": {
  "completion_tokens": 109,
  "prompt_tokens:": 20,
  "total_tokens": 129
  }
}
```

**返回**

| 参数 | 类型 | 描述 |
|------|------|------|
| rewards | list[float] | 通过@group_grader装饰器标记的多样本评分器。按照 trajectories 的顺序返回每个采样的得分 |
| reward | float | 通过@single_grader装饰器标记的单样本评分器。单个 trajectory 的得分 |
| metrics | Dict[str, float] | 支持返回 reward 过程的自定义指标，如计算耗时等。训练框架将把每个 step 的指标按 key 聚合出最大值，最小值和平均值。 |
| status | str | success：默认值<br>failure: 引擎重试三次仍然失败则任务失败<br>discard: 直接丢弃该样本<br>retry：引擎重试三次仍失败则丢弃该样本 |
| error | str | 如果status 为 failure，可携带具体失败原因或错误栈信息，会打印到训练日志中 |

**返回样例**

**rewards 样例：**
```json
[
  0.0,
  1.0,
  0.5
]
```

**reward 样例：**
```
0.5
```

**metrics 样例：**
```json
{
    "avg_length_reward": 0.49,
    "avg_formtat_reward": 0.9,
}
```

**常用Grader函数**

评分器的具体实现与效果定义和业务目标紧密相关，以下是一些常用的grader实现思路：

- **比较rollout结果和样本预设的答案**（通过训练集extra字段传入）
  - 全等/包含判定 可参考rl_demo中random_reward.py
  - 分别调用embedding模型计算相似度进行判定
  - 使用LLM进行语义比较并打分

- **RuleBase评分**
  - 输出格式判定
  - token/字符串长度惩罚
  - 根据过程耗时评分

- **调用外部服务/插件进行判定**
  - 调用Code sandbox运行代码，根据是否运行成功和运行结果与预设答案匹配度打分
  - 创建excel/数据库表，根据是否创建成功打分
  - 将输出的SQL语句用于查询指定数据库，根据是否能执行成功和结果是否符合预期打分

- **通过模型对单条样本进行打分**
  - LLM as a judge（基础模型 + prompt，可通过让模型在输出分值前思考并输出评分理由，提升评分准确性。同时也会增加训练服务的等待提高成本）可参考rl_search_mcp_demo中llm_grader.py
  - 训练并部署GRM（可通过让模型在输出分值前思考并输出评分理由，提升评分准确性。同时也会增加训练服务的等待提高成本）

- **对多条样本排序赋分/综合打分**
  - 对一组样本进行排序的难度低于对多条轨迹分别打出准确分值。

## 可观测性配置

### 轨迹分析

在精调参数配置job.py文件中配置enable_trajectory=True，即可开启轨迹分析功能。开启后，系统将记录强化学习训练轨迹，可视化展示于方舟控制台模型精调的轨迹分析功能下。此记录有助于模型效果分析与问题排查，对强化学习至关重要，建议训练前开启。

### 自定义函数日志

根据用户自定义需求记录Rollout、Reward函数执行中的关键信息，以精准定位问题、提高排查效率。
具体实现方面，在rollout.py文件内，通过logger.info、logger.error等方法完成日志记录操作。最终，这些日志将展示于方舟控制台模型精调的自定义日志功能模块下。

### 自定义效果指标

支持用户在 Grader 函数中自定义业务相关的评估指标，自定义后的指标将同步展示在方舟控制台模型精调的训练观测功能模块下，便于量化分析模型性能。

具体实现：在其metrics字段中补充自定义的键值对，将metrics与rewards、status、error一同封装至RewardFunctionResult对象中返回。

例如，可通过 NumPy 库计算奖励值的均值、标准差等统计指标并传入：

```python
import numpy as np
# 假设已计算得到奖励值列表rewards
metrics = {
    "test_mean": np.mean(rewards),  # 测试奖励平均值
    "test_std": np.std(rewards),    # 测试奖励标准差
    # 可按需添加其他自定义指标
}
```

## Plugin函数的测试

为确保 Plugin 函数功能符合预期，在提交强化学习任务前，需先进行本地测试，再提交在线 FaaS 测试，最后提交训练任务。

### 本地测试

在实现 rollout 函数的文件里，main 函数展示了如何在本地测试rollout函数和grader函数的结合使用。它支持用户进行单样本调试和多样本批量调试。

#### 单样本调试
测试时会基于样本，调用 demo_rollout 执行推理，获取模型的回答；将模型的回答和原始问题、正确答案一起传递给 llm_grader 进行评估，并打印评估结果。

#### 多样本调试
main 函数还展示了如何使用 test_with_dataset 函数对一个数据集中的多个样本进行批量推理和评估，以获取平均奖励分数。

```python
async def main():
    from ark_sdk.core.plugin.rollout.proxy import InferenceProxy, Mode
    from plugins.llm_grader import llm_grader
    import os

    # 调试模式，使用公共服务
    mode = Mode.Inference
    base_url = "https://ark.cn-beijing.volces.com/api/v3"
    api_key = os.getenv("ARK_API_KEY", "xxx")

    sample = ChatCompletionSample(
        **{
            "messages": [
                {
                    "role": "user",
                    "content": "通过景栗科技的私域运营服务和与薪勤科技的产品共创，哪两个公司在各自的领域实现了用户增长或应用上架？",
                }
            ],
            "thinking": {"type": "enabled"},
            "extra": {"answer": "景栗科技和薪勤科技", "prompt_id": "123"},
        }
    )
    proxy = InferenceProxy(sample, url=base_url, jwt_token=api_key, mode=mode)
    resp = await demo_rollout({}, proxy, sample)
    assert resp is None or resp.status == PluginStatus.SUCCESS, (
        f"rollout failed - {resp.error}"
    )
    logger.info(f"demo rollout done with result: {proxy.messages}")
    grader_res = await llm_grader(
        {},
        sample,
        [
            Trajectory(
                messages=proxy.messages,
                usage=proxy.usage,
                finish_reason=proxy.finish_reason,
                extra=resp.extra if resp else {},
            )
        ],
    )
    logger.info(f"demo grader done with result: {grader_res}")

    logger.info("small dataset")

    jsonl_file_path = "./data/search_dataset_dev_100.jsonl"
    from plugins.test_utils import test_with_dataset

    # NOTE: 可以针对其他模型测试数据集的reward分数，或者构建SFT数据集进行冷启动。正式提交任务前请使用此方法测试整体流程的并发能力，max_concurrent=batch_size，保证训练运行效率
    rewards = await test_with_dataset(
        jsonl_file_path,
        demo_rollout,
        llm_grader,
        api_key,
        base_url=base_url,
        limit=100,
        max_concurrent=16,
        n_sample=1,
        top_p=1,
        temperature=1.0,
    )
    logger.info(f"avg rewards: {sum(rewards) / len(rewards)}")
```

### 在线FaaS测试

完成本地测试后，用户可采用 SDK 或 CLI 方式拉起在线运行环境对plugin函数进行测试。需先完成 requirements.txt 的更新，具体要求及测试方法如下：

#### 更新requirements.txt
为避免 FaaS 环境装包时依赖自动升级引发异常，按以下规则生成requirements.txt：
本地环境验证通过后，推荐使用 uv 管理环境，执行`uv pip freeze > requirements.txt`固定间接依赖版本，过滤冗余依赖，精简依赖列表。

#### SDK方式用法

运行demo中test_faas.py文件，具体代码如下：

```python
from ark_sdk.resources.pipeline_plugin.test_instance import (
    get_or_create_pipeline_plugin_test_instance,
)
from ark_sdk.types.pipeline_plugin.rollout import (
    ChatCompletionSample,
)
from plugins.rollout import demo_rollout
from ark_sdk.core.plugin.rollout.proxy import InferenceProxy, Mode

if __name__ == "__main__":
    instance = get_or_create_pipeline_plugin_test_instance(demo_rollout)
    base_url = "https://ark.cn-beijing.volces.com/api/v3"
    api_key = os.getenv("ARK_API_KEY", "xxx")
    sample = ChatCompletionSample(
        **{
            "messages": [
                {
                    "role": "user",
                    "content": "北京天气",
                }
            ],
            "extra": {},
        }
    )
    proxy = InferenceProxy(sample, url=base_url, jwt_token=api_key, mode=Mode.Inference)
    resp = instance.request(
        {
            "context": {},
            "proxy": proxy,
            "sample": sample,
        },
        # sync 为false时不会创建新的faas函数（不会更新代码）
        sync=False,
    )
    print(resp)
```

#### CLI方式用法

通过 `ark test pipeline_plugin` 命令，快速测试强化学习精调的自定义 Plugin 函数。

##### 命令语法

```shell
ark test pipeline_plugin --fn <函数标识> --request <JSON请求体> [--sync]
```

##### 关键参数

| 参数 | 必填 | 说明 |
|------|------|------|
| --fn | 是 | 函数唯一标识，例：plugin.code_agent_grader:grader |
| --request | 是 | JSON 格式请求体，具体样例可参考SDK用法。 |
| --sync | 否 | 默认值为否，用于确定在执行前是否触发一次同步。 |

##### 完整示例

```shell
ark test pipeline_plugin --fn plugin.code_agent_grader:grader --request '{"sample":{...},"completion":{....}}' --sync
```

## 查看并管理精调任务

创建后的强化学习精调任务，支持通过 控制台 和 CLI 命令行 两种方式查看与管理，核心能力包括任务查询、详情查看、配置拉取、产物导出等。

### CLI 命令行方式

CLI命令整体使用格式为 `ark [verb] [noun] [arguments] [options]` ，常用命令说明如下表所示。

| 功能描述 | 命令语法 | 关键参数 / 选项 | 说明 |
|------|------|------|------|
| 查看所有精调任务 | ark list mcj [选项] | --page-size/-ps：单页返回数量，默认为 10<br>--page-number/-pn：查询页数，默认为 1<br>--customization_type/-t：限制查询的任务训练方式，多个训练方式逗号分隔，默认不限制<br>--phase/-p：限制查询的任务的阶段，多个训练方式逗号分隔，默认不限制 | 列举账号下的精调信息，包括精调任务ID，名称，训练方式，基础模型，现处阶段等。 |
| 获取单个任务详情 | ark get mcj <任务ID> | 任务ID:可通过控制台或查看所有精调任务命令获取 | 可获取精调任务详情：包括基任务的身份标识、当前状态、使用和生成模型信息、超参数、数据集位置、控制台链接 : 在网页上查看此任务的快捷入口。 |
| 拉取任务配置至本地 | ark pull mcj <任务ID> [选项] | --include-data/--exclude-data：用于选择是否拉取数据，默认不拉取数据。<br>--include-plugin/--exclude-plugin：用于选择是否拉取plugin代码，默认拉取。 | 本地修改后重新提交将生成新任务，不影响原任务。 |

#### 常用命令示例

```shell
# 1. 查看单页20条、第2页的RL类型精调任务
ark list mcj -ps 20 -pn 2 -t RL

# 2. 查询指定ID的任务详情
ark get mcj mcj-xxxxxx-xxx

# 3. 拉取任务配置、数据集及Plugin代码至本地
ark pull mcj mcj-xxxxxx-xxxxx --include-data --include-plugin
```
