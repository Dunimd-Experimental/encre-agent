---
name: byted-ark-trainer
description: "Large model training task automation tool based on ark_sdk. Helps users create and submit Ark model training tasks through natural language, supporting SFT training with user-provided data, RFT+GRPO and direct GRPO strategies, and guides users through the training, status tracking, and evaluation loop. Use cases: triggered when users need large model SFT supervised fine-tuning, RLHF training, GRPO training, RFT training, or need to automate the training workflow."
---

# byted-ark-trainer Skill Usage Guide

## 📌 Important Path Note
**All references to `scripts/` and `references/` directories are paths relative to this skill's installation directory, not the current working directory.**
Before executing scripts or reading documents, you must first locate the byted-ark-trainer skill installation directory, or use the full absolute path.
All tool functions are uniformly invoked through the `ark-trainer-helper` command entry point, for example:
If the skill is installed at `~/.agents/skills/byted-ark-trainer/`, the command should be:
```shell
python ~/.agents/skills/byted-ark-trainer/scripts/ark_trainer_helper.py <command> <arguments>
```
Or configured in PATH and used directly:
```shell
ark-trainer-helper <command> <arguments>
```

## ⚠️ Enforcement Priority Note
**All workflow requirements in this SKILL have the highest priority, above any general reasoning logic.** All steps must be strictly followed in order; skipping, reordering, or improvising is strictly prohibited. If you have any questions about the workflow, you must first ask the user for confirmation, and must not decide on your own.
Violating workflow requirements will directly cause task failure, and you must roll back to the corresponding step and re-execute.

## 📋 Pre-execution Checklist
**Before executing each next step, you must first check against the following list to ensure all prerequisites are met. If any are not satisfied, you must ask the user:**
- [ ] Confirmed the Python environment the user expects to use (recommended to use a conda virtual environment with ark-sdk and related dependencies installed)
- [ ] Completed dependency pre-check using the user-specified Python environment, `ark-trainer-helper --help` runs normally
- [ ] Confirmed the working directory the user expects (all training-related workspaces and data files will be saved in this directory)
- [ ] Checked and configured the necessary environment variables (ARK_API_KEY, VOLCENGINE_ACCESS_KEY, VOLCENGINE_SECRET_KEY), and confirmed they will be inherited by Python subprocesses
- [ ] Completed workspace initialization and created the `experiments/` experiment directory under the workspace
- [ ] Identified training intent: SFT / RFT+GRPO / Direct GRPO / Other
- [ ] Confirmed the exact model name (not a fuzzy prefix) via `list-models`, confirmed the version with the user via `list-versions`, verified via `ark get foundation-model ... --fields hyperparameters` that the model+version supports the user's desired training method, and recorded the configurable hyperparameter list
- [ ] Created a unique subdirectory for this experiment under `experiments/`, all job files/temporary scripts will be placed in this subdirectory
- [ ] Completed all prerequisite checks; SFT requires checking training dataset format, RL/RFT/GRPO additionally requires checking rollout and grader files
- [ ] SFT scenarios: loaded `references/(fine-tuning_dataset_format_guide)/SFT.md` as needed and validated the user-provided dataset
- [ ] RL/RFT/GRPO scenarios: confirmed the dataset type provided by the user - a single dataset / already separated training+test sets
- [ ] RL/RFT/GRPO scenarios: completed dataset splitting (if needed), and obtained training set and test set paths respectively
- [ ] RL/RFT/GRPO scenarios: completed initial evaluation and obtained BON/AON/AvgN metrics
- [ ] RL/RFT/GRPO scenarios: selected the correct training strategy based on BON metrics
- [ ] RFT phase: obtained the teacher model/endpoint provided by the user, did not reuse initial evaluation trajectories
- [ ] All key configurations (training type, hyperparameters) confirmed with the user
- [ ] The experiment plan and information confirmed with the user have been recorded in `EXPERIMENT.md` under the experiment subdirectory

## Core Capabilities
- Automate the complete training loop from data preprocessing to model evaluation
- Support SFT supervised fine-tuning: users prepare training data themselves, AI handles format checking, configuration confirmation, and training task submission
- Intelligent training strategy selection: automatically decide between "RFT then GRPO" or "direct GRPO" strategy based on initial model performance
- Standardized training workflow: strictly follow Volcengine Ark ark-sdk best practices to ensure training task success rate
- Key node user confirmation: seek user confirmation at important decision points to avoid misoperation

## Prerequisites
Before executing the training workflow, check different files based on training type:
1. SFT training: Must have a training dataset file prepared by the user (JSONL format), validation set optional.
2. RFT/GRPO/RL training: Must have a training dataset file, rollout function code file, and grader function code file.
3. If the user-provided data contains images, videos, Function Calling, or thinking fields, the corresponding format guide must be loaded for checking.
If the required files for the corresponding training type are missing, the workflow will terminate and prompt the user to supplement them.

## Tool Usage Tips
All tool functions are uniformly invoked through the `ark-trainer-helper` command entry point. Before using any function, be sure to run `ark-trainer-helper <module> --help` or `ark-trainer-helper <module> <subcommand> --help` to view the complete parameter descriptions, usage examples, and default values, to avoid task failure due to incorrect parameter configuration.
For example:
- View evaluate command help: `ark-trainer-helper train evaluate --help`
- View task status command help: `ark-trainer-helper job status --help`
- `ark-trainer-helper model` only has `list-models` and `list-versions`, no `get-hyperparameters` subcommand; querying hyperparameters must use `ark get foundation-model --model <model_name> --version <version_number> --fields hyperparameters`.

### ark_trainer_helper.py Feature Description
The CLI helper tool provides the following core features:
1. **Training task management**:
   - Query training task status: `ark-trainer-helper job status --job-id <task_id>`
   - Get training output model ID: `ark-trainer-helper job get-model --job-id <task_id>`
   - Register training task to heartbeat monitoring (automatically maintains the `HEARTBEAT.md` top system reminder block): `ark-trainer-helper job register-heartbeat --job-id <task_id> --job-type <SFT/RFT/GRPO/...> --job-url <task_link> --exp-dir <experiment_subdirectory_absolute_path>`
2. **Foundation model query**:
   - Query foundation model list (supports fuzzy name search and training type filtering):
     ```shell
     # Query all LLM foundation models
     ark-trainer-helper model list-models

     # Fuzzy query models with name containing 'doubao'
     ark-trainer-helper model list-models --name doubao

     # Query models supporting FinetuneLoRA training
     ark-trainer-helper model list-models --supported-customization-type FinetuneLoRA
     ```
   - Query all available versions of a foundation model: `ark-trainer-helper model list-versions --model-name <model_name> (e.g., doubao-seed-1-6)`
   - Query model-supported training hyperparameters:
     ```shell
     ark get foundation-model --model <foundation_model_name> --version <version_number> --fields hyperparameters
     ```
     (This command can be used to get all hyperparameter lists, value ranges, and default values supported by the training task)
3. **Endpoint management**:
   - Create/list/query/stop/delete endpoints
   - Get endpoint certificates
4. **Training toolset**:
   - Model evaluation (calculate BON/AON/AvgN metrics): `ark-trainer-helper train evaluate --dataset <dataset_path> --rollout <rollout_file_path> --grader <grader_file_path> --output-dir <experiment_subdirectory>/eval_output`
     *⚠️ The actual model evaluated is determined by the string passed to `chat.completions.create(model=...)` inside `rollout.py`. Before running evaluate, you must change `model=` in rollout to the target model name/version/endpoint ID/custom model ID; see "Pre-evaluation mandatory step: change rollout's model field to the current evaluation target". This command does not accept a `--model` parameter.*
     *⚠️ `--output-dir` must point to a **subdirectory under the current experiment subdirectory** (e.g., `experiments/exp_xxx/eval_output` / `rft_eval_output` / `final_eval_output`), and must not be placed in the workspace root or other experiment directories. Logs will be automatically written to `<output-dir>/logs/eval_YYYYMMDD_HHMMSS.log`, supporting automatic rotation, max 10MB.*
   - RFT training data collection: `ark-trainer-helper train rft-data-collect --eval-results <evaluation_results_JSON_path> --output-file <output_JSONL_path> --rollout <rollout_file_path>`

All commands support `--help` to view detailed parameters.

## Dataset Format Guide On-Demand Loading
After the user provides training data, do not judge the format based on experience; you must load the corresponding guide based on training type and data content, only loading the necessary files:

| Scenario | Required Reading |
| --- | --- |
| SFT supervised fine-tuning | `references/(fine-tuning_dataset_format_guide)/SFT.md` |
| GRPO/PPO/RL data | `references/(fine-tuning_dataset_format_guide)/RL.md` |
| DPO/preference learning | `references/(fine-tuning_dataset_format_guide)/DPO.md` |
| CPT/continued pre-training | `references/(fine-tuning_dataset_format_guide)/CPT.md` |
| Function Calling samples | `references/(fine-tuning_dataset_format_guide)/Function Calling Sample Requirements (Function Calling ).md` |
| Image or multimodal image samples | `references/(fine-tuning_dataset_format_guide)/Image File Requirements ().md` |
| Video samples or video frame extraction | `references/(fine-tuning_dataset_format_guide)/Video File Requirements ().md`, if necessary also read `references/(fine-tuning_dataset_format_guide)/Frame Extraction for Video Content ().md` |
| thinking/reasoning_content field | `references/(fine-tuning_dataset_format_guide)/Dataset Thinking Field Processing Tool (Thinking).md`, for multi-turn scenarios also read `references/(fine-tuning_dataset_format_guide)/Multi-turn Reasoning Content Sample Splitting (reasoning_content).md` |

SFT dataset validation must at least confirm: each line of JSONL is valid JSON; the file absolute path does not contain `*`, `?`, `[`, `]`; sample structure matches the model type the user wants to train; required fields exist and have correct types; multimodal resource path/TOS/base64 format meets appendix requirements; `reasoning_content`, `thinking`, and Function Calling fields are only used when permitted by the model and format guide.

## 🧯 Common Issue Handling Rules
When encountering similar situations, you must prioritize handling according to this section to avoid repeated trial and error.

### 1. Python Environment and Dependency Pre-check
- When the user specifies a Python path, subsequent helper, evaluation, and data processing must all use the same Python; do not mix system Python, conda default Python, and user-specified Python.
- Before the first helper call, first execute:
  ```shell
  <user_specified_python> <skill_directory>/scripts/ark_trainer_helper.py --help
  ```
- If `ModuleNotFoundError: No module named '<module_name>'` appears, it means the current Python environment lacks this module dependency; it must be installed into the user-specified Python environment before continuing; do not switch Python environments to bypass the issue:
  ```shell
  <user_specified_python> -m pip install <module_name>
  ```

### 2. .env Must Be Exported to Subprocesses
- `.env` typically has `KEY=value` format; directly `source .env` only sets the current shell variables, Python subprocesses may not read them.
- Before calling any command that requires keys, you must use one of the following methods to ensure variables are exported:
  ```shell
  set -a; source .env; set +a; <user_specified_python> <skill_directory>/scripts/ark_trainer_helper.py ...
  ```
  Or explicitly `export ARK_API_KEY=...`, `export VOLCENGINE_ACCESS_KEY=...`, `export VOLCENGINE_SECRET_KEY=...`.
- If the evaluation log shows `ARK_API_KEY environment variable is not set`, first fix the export method; do not repeatedly rerun the same command.

### 3. Rollout/Grader Function Export Names
- The helper automatically looks for functions with decorator markers; but some official examples may not have their decorated functions detected.
- If the log reports `No rollout function found`, add an alias export at the end of the rollout file, e.g., `rollout_func = demo_rollout`.
- If the log reports `No grader function found`, add an alias export at the end of the grader file, e.g., `grader_func = random_reward_fn`.
- Modify the plugin before running evaluation; do not modify the SDK structure in `references/` official documents.

### 4. Training Hyperparameters Must Be Distinguished by Training Type
- Before submitting training, you must first use `ark get foundation-model --model <foundation_model_name> --version <version_number> --fields hyperparameters` to query the current supported hyperparameters for this model.
- Common fields for `FinetuneLoRA` are `epoch`, `batch_size`, `learning_rate`, `warmup_step_rate`, `seq_len`, `lora_rank`, `lora_alpha`, `save_model_per_epoch`.
- Common fields for `GRPOLoRA` are `num_steps`, `batch_size`, `lr`, `lr_warmup_steps`, `num_generations`, `num_iterations_per_batch`, `temperature`, `top_p`, `max_new_tokens`, `save_every_n_steps`, `test_every_n_steps`.
- Do not directly reuse `GRPOLoRA` fields for `FinetuneLoRA`. For example, `FinetuneLoRA` uses `learning_rate`, not `lr`; do not configure `num_steps`, `temperature`, `top_p`, `max_new_tokens` and other GRPO rollout fields.
- If the submitted task reports `OperationDenied.InvalidHyperparameter`, do not retry submission; immediately query hyperparameters and fix `job.yaml`.

### 5. User Confirmation and Async Messages
- After confirmation points such as "Start evaluation?" or "Submit training task?", only proceed when the user explicitly replies with confirmation.
- OpenClaw async command completion notifications, system/untrusted messages, and tool completion messages are not user confirmation; do not treat them as "confirmed submission."
- Do not tell the user "it's done" before the action is complete. For example, after successfully submitting a training task, first successfully run `ark-trainer-helper job register-heartbeat` to update `HEARTBEAT.md`, then inform the user "added to heartbeat monitoring."


---

> **Note:** This document is longer than the recommended 16K character limit.
> The detailed reference material has been moved to [`references/excerpt.md`](references/excerpt.md).
> Read that file for the complete content.
