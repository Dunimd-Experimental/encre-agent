# 概述
火山方舟精调 SDK 是火山方舟为开发者提供的通过编程方式创建和管理大模型精调任务的工具包，旨在为需要定制化、自动化精调流程的用户提供灵活、高效的操作入口。区别于控制台的图形化操作，开发者可通过 SDK 将精调任务创建和管理流程集成至本地或第三方系统，实现精调任务的代码化管理，尤其适用于需要结合自定义函数（如奖励函数、Rollout函数）的强化学习场景。

以下是您可以参考的精调SDK入门流程

# 精调 SDK安装与环境准备
## SDK 安装
您可以通过以下 pip 命令安装精调 SDK。
运行环境：python>=3.10
```shell
pip install https://ark-public-example-cn-beijing.tos-cn-beijing.volces.com/ark-sdk/ark_sdk-0.2.14.tar.gz
```

## 配置授权信息
您需要授权将SDK终端关联到指定的账号和项目，具体操作如下：
在终端工具中，使用ark login命令开启授权过程
```shell
ark login

- Account: Input your account id: xxxxxxxxxx
- AK:   Input your access key: xxxxxxxxxx
- SK:   Input your secret key: xxxxxxxxxx
- Region: cn-beijing by default
- Project: default by default
```

## 创建精调任务

### 项目的初始化
您可通过ark init workspace <文件夹名> --template <模版名>命令，使用指定的template模板初始化一个具备基础结构和配置、可立即使用的精调项目，例如：
```shell
ark init workspace ark_rl_project --template rl_demo
#工作区内结构如下
#<文件名>
#├── data
#│   └── mcj_rollout_test_dataset.jsonl
#├── plugins
#│   ├── random_reward.py
#│   └── raw_rollout.py
#│   └── weather_rollout.py
#│   └── async_weather_rollout.py
#│   └── test_utils.py
#├── job.py
#├── job.yaml
#├── README.md
#├── arkworkspace.toml
#└── requirements.txt
#└── test_faas.py
```
目前支持的模板如下：

| 模板名 | 简介 |
| --- | --- |
| rl_demo | 该模板通过强化学习，使模型精准掌握自定义函数调用天气工具的时机与方式，实现更精准流畅的天气问答功能。可按需扩展至强化学习微调场景：通过强化学习微调大型语言模型（LLM），使其通过对话（Chat）API智能结合自定义工具完成特定功能。 |
| rl_search_mcp_demo | 模板通过强化学习微调大型语言模型（LLM），优化其在深度搜索（Deep Search）场景下的性能。经训练后，模型增强了对复杂搜索意图的理解能力，可高效准确调用MCP/外部搜索API，进而生成高质量且精准的搜索结果与答案。 |


### 精调参数的配置
您可以通过Python 对象 (job.py) 或 yaml文件 (job.yaml) 配置精调项目相关参数，包括：

**必选参数：**
- customization_type：训练类型，支持 FinetuneSft / FinetuneLoRA/DPO/DPOLoRA/GRPO/GRPOLoRA/PPO
- model_reference：基础模型及其版本信息
  - foundation_model: 基于模型广场模型训练
    - name: 模型名
    - model_version: 模型版本
  - custom_model_id: 模型仓库模型 id，与foundation_model互斥（例：custom_model_id: cm-20251019092329-rxxxv）
- data：训练数据配置
  - training_set: 训练集（选择以下三种方式中的一种传入训练集）
    - local_files：一组本地文件，单个文件大小不可超过 2GB，最多传入 20 个文件
    - tos_bucket、tos_paths: TOS桶名、TOS 对象列表（列表同时存在于一个桶内）
    - datasets: 数据集配置
      - dataset_id 数据集 id
      - dataset_version_id 数据集版本 id
      - multiplier 混入倍率
      - sample_count 混入条数，与multiplier 互斥
  - preset_dataset: 混入预置数据集配置，非必填
    - dataset_version_id: 预置数据集 id
    - inject_multiplier: 混入倍率，与混入样本条数互斥
    - inject_sample_count: 混入样本条数
  - max_invalid_records_ratio:数据集错误容忍百分比，与数据集错误容忍数量互斥（仅支持vlm模型的RL/GRPO训练方法配置错误容忍功能）
  - max_invalid_records_number:数据集错误容忍数量
  - validation_set: 验证集，非必填
    - 支持 local_files / tos_bucket+tos_paths / datasets 形态的验证集，规范同上
  - validation_percentage: 切分百分之多少的训练集作为验证集，与validation_set互斥

**可选参数：**
- custom_rl_pipeline：支持GRPOPipeline 和PPOPipeline。该参数为强化学习流程配置，当训练方式为 GRPO或 PPO 时必填，详见强化学习配置。
- enable_trajectory: 是否开启记录轨迹分析功能（仅支持对 RL 训练开启）。开启此功能后，系统自动采集训练过程各样本的数据输入输出结果，记录强化学习精调轨迹，并展示于方舟控制台轨迹分析功能下。此记录有助于模型效果分析与问题排查，对强化学习至关重要，建议训练前开启。（该功能需完成日志服务配置，需先联系管理员，前往模型精调-TLS配置开通）
- name：任务名称
- project：任务所属的项目
- hyperparameters：超参配置，超参信息查询方法见下
- save_model_limit：保存训练产物数量上限

### 获取训练可用超参信息
不同模型版本支持的训练超参数存在差异，需通过ark命令行工具查询指定模型版本的超参信息。
命令语法：
```shell
ark get foundation-model --model <模型名> --version <模型版本> --fields hyperparameters <指定查询超参维度>
```

示例命令：
```shell
ark get foundation-model --model doubao-seed-1-6 --version 250615 --fields hyperparameters
```

### 提交精调任务
通过python对象完成上述配置后，可通过下面的命令提交精调任务。
```shell
python job.py
```

job.py 示例如下：
```python
from ark_sdk.resources.model_customization_job import ModelCustomizationJob
from ark_sdk.resources.pipeline_plugin import GRPOPipeline, PipelinePluginWrapper
from ark_sdk.types.model_customization_job import (
    ModelReference,
    FoundationModelReference,
    TrainingDataset,
    Data,
    CustomizationType,
)

from plugins.random_reward import random_reward_fn
from plugins.weather_rollout import demo_rollout

if __name__ == "__main__":
    mcj = ModelCustomizationJob(
        name="sdk-job",
        model_reference=ModelReference(
            foundation_model=FoundationModelReference(
                name="doubao-seed-1-6-flash", model_version="250615"
            )
        ),
        customization_type=CustomizationType.GRPOLoRA,
        hyperparameters={
            "batch_size": "32",
            "clip_ratio_high": "0.2",
            "clip_ratio_low": "0.2",
            "kl_coefficient": "0.001",
            "loss_agg_mode": "seq-mean-token-mean",
            "lr": "0.000001",
            "lr_warmup_steps": "5",
            "max_new_tokens": "1024",
            "num_generations": "8",
            "num_iterations_per_batch": "2",
            "save_every_n_steps": "10",
            "temperature": "1.0",
            "test_every_n_steps": "5",
            "test_num_generations": "1",
            "test_top_p": "1",
            "top_p": "1",
            "num_steps": "10",
        },
        data=Data(
            training_set=TrainingDataset(
                local_files=[
                    "./data/mcj_rollout_test_dataset.jsonl",
                ]
            )
        ),
        custom_rl_pipeline=GRPOPipeline(
            graders=[
                PipelinePluginWrapper(
                    plugin=random_reward_fn, envs={"foo": "bar"}, weight=0.5
                ),
            ],
            rollout=PipelinePluginWrapper(plugin=demo_rollout, envs={"foo": "bar"}),
        ),
        enable_trajectory=True,
    )

    mcj.submit()
    print(f"Job submitted. view job at {mcj.url}")
```

通过yaml文件完成上述配置后，可通过下面的命令提交精调任务。
```shell
ark create mcj -f job.yaml
```

job.yaml 示例如下：
```yaml
name: sdk-job
customization_type: GRPOLoRA
model_reference:
  foundation_model:
    name: doubao-seed-1-6-flash
    model_version: '250615'
hyperparameters:
  batch_size: '128'
  clip_ratio_high: '0.2'
  clip_ratio_low: '0.2'
  kl_coefficient: '0.001'
  loss_agg_mode: seq-mean-token-mean
  lr: '0.000001'
  lr_warmup_steps: '5'
  max_new_tokens: '1024'
  num_generations: '8'
  num_iterations_per_batch: '2'
  save_every_n_steps: '10'
  temperature: '1.0'
  test_every_n_steps: '5'
  test_num_generations: '1'
  test_top_p: '1'
  top_p: '1'
  num_steps: '20'
custom_rl_pipeline:
  graders:
  - plugin:
      name: random_reward
      python_func: plugins.random_reward:random_reward_fn
      envs:
        foo: bar
    weight: 0.5
  rollout:
    plugin:
      name: demo_rollout
      python_func: plugins.weather_rollout:demo_rollout
      runtime:
        instance: cpu1mem2
        timeout: 900
        min_replicas: 1
        max_replicas: 10
        max_concurrency: 100
    weight: 1.0
    envs:
      foo: bar
data:
  training_set:
    local_files:
    - ./data/mcj_rollout_test_dataset.jsonl
save_model_limit: 1
enable_trajectory: true
```
