# 训练任务配置模板索引

这个目录下的模板是经过实测校验、可以直接复制到实验子目录使用的起点文件。**编写任何新的 `job.yaml` 或 `job.py` 之前，必须先从这里挑对应模板复制，再按实际情况改值**，严禁从零手写。

| 训练方式 | YAML 模板 | Python 模板 | 适用场景 |
| --- | --- | --- | --- |
| SFT（LoRA，推荐默认） | [`job_sft_lora.yaml`](./job_sft_lora.yaml) | [`job_sft_lora.py`](./job_sft_lora.py) | SFT 监督微调 / RFT 阶段的训练任务 |
| GRPO（LoRA，推荐默认） | [`job_grpo_lora.yaml`](./job_grpo_lora.yaml) | [`job_grpo_lora.py`](./job_grpo_lora.py) | GRPO 强化学习训练（直接 GRPO 或 RFT 之后继续 GRPO） |

全量训练（`FinetuneSft` / `GRPO`）目前不提供独立模板。若用户明确要求全量训练，从对应的 LoRA 模板起步并把 `customization_type` 替换为 `FinetuneSft` / `GRPO` 即可；其余字段结构相同，但需重新用 `ark get foundation-model --fields hyperparameters` 查询对应小节的超参数白名单。

## 使用流程

1. 完成 Step 2.5「基础模型与训练方式确认」，拿到精确模型名、版本、该方式的超参数白名单。
2. 把本目录对应模板复制到实验子目录（例如 `experiments/exp_xxx/job.yaml` 或 `job.py`）。
3. 按本次实验改：`name`、`model_reference`、`data`、`hyperparameters`、以及 GRPO 的 `custom_rl_pipeline`。
4. **hyperparameters 只允许保留白名单内的字段**；白名单外的字段（无论是模板里默认带的、还是凭印象加上的）必须删掉。
5. YAML 用 `ark create mcj -f job.yaml` 提交；Python 用 `python job.py` 提交。

## 两种写法的选择建议

- **YAML**：字段直观、改值方便、可版本化，是首选。
- **Python**：需要在提交前做条件判断、多任务批量提交、或想直接引用 rollout/grader 的 Python 函数对象时使用。GRPO 场景下 Python 脚本可以直接 `from plugins.xxx import yyy` 引用函数，不用再手填字符串路径，更不易出错。**必须使用安装了 ark_sdk 的虚拟环境 Python 执行**，例如 `/path/to/your/env/bin/python job.py`。

## 常见踩坑速查

- `model_version` 必须是**字符串**（`'250615'` / `"250615"`），不是整数。
- `data.training_set` 必须是对象，且至少含 `local_files` / `tos_bucket` / `datasets` 之一；不能直接 `training_set: <路径>` 字符串。
- SFT 字段是 `learning_rate`，GRPO 字段是 `lr`，**不可互换**。
- SFT 绝不能写 `custom_rl_pipeline` / `enable_trajectory`；GRPO 必须写 `custom_rl_pipeline`。
- 提交命令是 `ark create mcj`，不是 `ark create customization-job`。
- `hyperparameters` 中出现任何非查询白名单字段都会被拒或静默生效，必须先删掉。
