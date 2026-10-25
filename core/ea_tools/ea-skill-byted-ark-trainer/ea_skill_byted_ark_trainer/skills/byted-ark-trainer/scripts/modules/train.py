from __future__ import annotations

"""
训练相关工具模块
包含评估和RFT数据收集功能
"""

import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from dataclasses import dataclass, field

from loguru import logger

# ARK SDK imports
from ark_sdk.core.plugin.rollout.proxy import InferenceProxy, Mode


@dataclass
class EvalConfig:
    """Evaluation configuration.

    注意：评估的模型名/端点ID/自定义模型ID **不在这里**配置，而是由 rollout 函数内部
    调用 `chat.completions.create(model=...)` 时传入的那个字符串决定。运行 evaluate
    前请先把 rollout.py 中的 model 字段改成目标模型。
    """

    n_rollouts: int = 8
    max_concurrency: int = 15
    batch_size: int = 15

    # Model service configuration
    model_service: Dict = field(
        default_factory=lambda: {
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": os.environ.get("ARK_API_KEY", ""),
        }
    )

    # Grader configuration
    graders: List[Dict] = field(default_factory=list)

    # Rollout configuration
    rollout: Dict = field(
        default_factory=lambda: {"python_func": "rollout:rollout_func", "envs": {}}
    )


def load_dataset(dataset_path: str) -> List[Dict]:
    """
    Load dataset from JSON or JSONL file.

    Args:
        dataset_path: Path to the dataset file

    Returns:
        List of data samples
    """
    data = []
    path = Path(dataset_path)

    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {dataset_path}")

    # Try loading as JSON
    try:
        with open(dataset_path, "r", encoding="utf-8") as f:
            content = json.load(f)
            if isinstance(content, list):
                data = content
            else:
                data = [content]
        return data
    except json.JSONDecodeError:
        pass

    # Load as JSONL
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse line: {e}")

    return data


def load_rollout_function(rollout_path: str):
    """
    Load rollout function from Python file.

    Args:
        rollout_path: Path to the rollout function file

    Returns:
        Rollout function
    """
    path = Path(rollout_path)

    if not path.exists():
        raise FileNotFoundError(f"Rollout file not found: {rollout_path}")

    # Add parent directory to path
    sys.path.insert(0, str(path.parent))

    # Import the module
    module_name = path.stem
    module = __import__(module_name)

    # First look for functions decorated with @rollout, @single_rollout, or @group_rollout
    rollout_functions = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if callable(attr):
            # Check if function has rollout decorator markers
            if (
                hasattr(attr, "_is_rollout")
                or hasattr(attr, "_is_single_rollout")
                or hasattr(attr, "_is_group_rollout")
            ):
                rollout_functions.append((attr_name, attr))
            # Fallback: check for decorated function wrapper
            elif hasattr(attr, "__wrapped__"):
                # Check if the wrapped function is a rollout
                wrapped = getattr(attr, "__wrapped__")
                if (
                    hasattr(wrapped, "_is_rollout")
                    or hasattr(wrapped, "_is_single_rollout")
                    or hasattr(wrapped, "_is_group_rollout")
                ):
                    rollout_functions.append((attr_name, attr))

    if len(rollout_functions) == 1:
        logger.info(f"Found rollout function '{rollout_functions[0][0]}' via decorator")
        return rollout_functions[0][1]
    elif len(rollout_functions) > 1:
        function_names = [name for name, _ in rollout_functions]
        raise AttributeError(
            f"Multiple rollout functions found in {rollout_path}: {', '.join(function_names)}. "
            f"Please ensure only one function is decorated with @rollout, @single_rollout, or @group_rollout."
        )

    # Fallback to function name matching if no decorated functions found
    if hasattr(module, "rollout_func"):
        return getattr(module, "rollout_func")
    elif hasattr(module, "rollout"):
        return getattr(module, "rollout")
    else:
        raise AttributeError(
            f"No rollout function found in {rollout_path}. Please ensure either:\n"
            f"1. A function is decorated with @rollout, @single_rollout, or @group_rollout, OR\n"
            f"2. A function named 'rollout_func' or 'rollout' exists in the file."
        )


def load_grader_function(grader_path: str):
    """
    Load grader function from Python file.

    Args:
        grader_path: Path to the grader function file

    Returns:
        Grader function
    """
    path = Path(grader_path)

    if not path.exists():
        raise FileNotFoundError(f"Grader file not found: {grader_path}")

    # Add parent directory to path
    sys.path.insert(0, str(path.parent))

    # Import the module
    module_name = path.stem
    module = __import__(module_name)

    # First look for functions decorated with @grader, @single_grader, or @group_grader
    grader_functions = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if callable(attr):
            # Check if function has grader decorator markers
            if (
                hasattr(attr, "_is_grader")
                or hasattr(attr, "_is_single_grader")
                or hasattr(attr, "_is_group_grader")
            ):
                grader_functions.append((attr_name, attr))
            # Fallback: check for decorated function wrapper
            elif hasattr(attr, "__wrapped__"):
                # Check if the wrapped function is a grader
                wrapped = getattr(attr, "__wrapped__")
                if (
                    hasattr(wrapped, "_is_grader")
                    or hasattr(wrapped, "_is_single_grader")
                    or hasattr(wrapped, "_is_group_grader")
                ):
                    grader_functions.append((attr_name, attr))

    if len(grader_functions) == 1:
        logger.info(f"Found grader function '{grader_functions[0][0]}' via decorator")
        return grader_functions[0][1]
    elif len(grader_functions) > 1:
        function_names = [name for name, _ in grader_functions]
        raise AttributeError(
            f"Multiple grader functions found in {grader_path}: {', '.join(function_names)}. "
            f"Please ensure only one function is decorated with @grader, @single_grader, or @group_grader."
        )

    # Fallback to function name matching if no decorated functions found
    if hasattr(module, "grader_func"):
        return getattr(module, "grader_func")
    elif hasattr(module, "grader"):
        return getattr(module, "grader")
    elif hasattr(module, "reward_func"):
        return getattr(module, "reward_func")
    else:
        raise AttributeError(
            f"No grader function found in {grader_path}. Please ensure either:\n"
            f"1. A function is decorated with @grader, @single_grader, or @group_grader, OR\n"
            f"2. A function named 'grader_func', 'grader', or 'reward_func' exists in the file."
        )


class BONEvaluator:
    """
    Best of N (BON) Evaluator

    Evaluates a model using multiple rollouts per sample and calculates:
    - BON (Best of N): Proportion with at least one correct rollout
    - AON (All of N): Proportion with all rollouts correct
    - AvgN (Average N): Average accuracy across all rollouts
    """

    def __init__(self, config: EvalConfig):
        self.config = config

    async def evaluate(
        self,
        dataset: List[Dict],
        rollout_func,
        grader_func,
        output_dir: str = "./eval_output",
    ) -> Dict[str, Any]:
        """
        Run BON evaluation with async concurrency.

        Args:
            dataset: List of data samples
            rollout_func: Rollout function (async)
            grader_func: Grader function (async)
            output_dir: Output directory for results

        Returns:
            Dictionary with BON, AON, AvgN scores and detailed results
        """

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        n_rollouts = self.config.n_rollouts
        max_concurrency = self.config.max_concurrency

        logger.info(
            f"Starting BON evaluation with {len(dataset)} samples, {n_rollouts} rollouts each"
        )
        logger.info(f"Max concurrency: {max_concurrency}")

        # Semaphore to control concurrency
        sem = asyncio.Semaphore(max_concurrency)

        # Results storage
        results = [None] * len(dataset)  # Pre-allocate for order preservation

        async def run_single_rollout(
            sample_idx: int, sample: Dict, rollout_idx: int
        ) -> Dict:
            """Run a single rollout and grade the result."""
            async with sem:  # Acquire semaphore to limit concurrency
                try:
                    # Import required types
                    from ark_sdk.types.pipeline_plugin.rollout import (
                        ChatCompletionSample,
                        Trajectory,
                    )

                    # Create ChatCompletionSample from dataset sample
                    if "messages" in sample:
                        chat_sample = ChatCompletionSample(
                            messages=sample["messages"], extra=sample.get("extra", {})
                        )
                    else:
                        chat_sample = ChatCompletionSample(**sample)

                    # Initialize InferenceProxy
                    proxy = InferenceProxy(
                        chat_sample,
                        url=self.config.model_service["base_url"],
                        jwt_token=self.config.model_service["api_key"],
                        mode=Mode.Inference,
                    )

                    # Run rollout
                    resp = await rollout_func({}, proxy, chat_sample)

                    # Grade the result
                    trajectory = Trajectory(
                        messages=proxy.messages,
                        usage=proxy.usage,
                        finish_reason=proxy.finish_reason,
                        extra=resp.extra if resp else {},
                    )
                    grader_res = await grader_func({}, chat_sample, [trajectory])
                    reward = (
                        grader_res.rewards[0]
                        if grader_res and grader_res.rewards
                        else 0
                    )

                    is_correct = reward == 1.0 or reward == 1

                    # 
                    messages_data = []
                    for msg in proxy.messages:
                        if hasattr(msg, "model_dump"):
                            msg_dict = msg.model_dump()
                        elif isinstance(msg, dict):
                            msg_dict = msg.copy()
                        else:
                            msg_dict = {
                                "role": getattr(msg, "role", ""),
                                "content": getattr(msg, "content", ""),
                            }
                        messages_data.append(msg_dict)

                    return {
                        "sample_idx": sample_idx,
                        "rollout_idx": rollout_idx,
                        "result": resp.model_dump()
                        if resp and hasattr(resp, "model_dump")
                        else str(resp),
                        "messages": messages_data,
                        "trajectory": trajectory.model_dump()
                        if hasattr(trajectory, "model_dump")
                        else str(trajectory),
                        "reward": reward,
                        "correct": is_correct,
                    }

                except Exception as e:
                    logger.warning(
                        f"Rollout {rollout_idx} failed for sample {sample_idx}: {e}"
                    )
                    return {
                        "sample_idx": sample_idx,
                        "rollout_idx": rollout_idx,
                        "error": str(e),
                        "correct": False,
                    }

        # Build all tasks: each sample needs n_rollouts
        all_tasks = []
        for sample_idx, sample in enumerate(dataset):
            for rollout_idx in range(n_rollouts):
                # Store metadata with the task for result aggregation
                task = run_single_rollout(sample_idx, sample, rollout_idx)
                all_tasks.append((sample_idx, rollout_idx, task))

        # Run all tasks concurrently with progress bar
        logger.info(f"Total tasks to run: {len(all_tasks)}")
        task_coros = [t[2] for t in all_tasks]

        # Use asyncio.gather with return_exceptions=True to handle errors gracefully
        # This runs all tasks concurrently with semaphore control inside each task
        # Show progress bar
        from tqdm.asyncio import tqdm_asyncio

        all_results = []
        for f in tqdm_asyncio.as_completed(
            task_coros, total=len(task_coros), desc="Evaluating"
        ):
            result = await f
            all_results.append(result)

        # Map results back to samples
        sample_results_map = {
            i: {"sample_idx": i, "data": dataset[i], "rollouts": [], "correct_count": 0}
            for i in range(len(dataset))
        }

        for result in all_results:
            # Handle exceptions
            if isinstance(result, Exception):
                logger.warning(f"Rollout task failed: {result}")
                result = {
                    "sample_idx": -1,
                    "rollout_idx": -1,
                    "error": str(result),
                    "correct": False,
                }

            sample_idx = result.get("sample_idx")
            if sample_idx not in sample_results_map:
                logger.warning(f"Rollout result has invalid sample_idx: {sample_idx}")
                continue

            sample_results_map[sample_idx]["rollouts"].append(result)
            if result.get("correct", False):
                sample_results_map[sample_idx]["correct_count"] += 1

        # Convert to list in order
        results = [sample_results_map[i] for i in range(len(dataset))]

        # Log progress
        logger.info(
            f"Completed all {len(all_tasks)} rollouts for {len(dataset)} samples"
        )

        # Calculate metrics
        bon_count = 0  # At least one correct
        aon_count = 0  # All correct
        total_correct = 0
        total_rollouts = 0

        for result in results:
            correct_count = result["correct_count"]
            n_roll = len(result["rollouts"])

            # BON: at least one correct
            if correct_count > 0:
                bon_count += 1

            # AON: all correct
            if correct_count == n_roll and n_roll > 0:
                aon_count += 1

            total_correct += correct_count
            total_rollouts += n_roll

        n_samples = len(results)

        metrics = {
            "bon_score": bon_count / n_samples if n_samples > 0 else 0.0,
            "aon_score": aon_count / n_samples if n_samples > 0 else 0.0,
            "avgn_score": total_correct / total_rollouts if total_rollouts > 0 else 0.0,
            "total_samples": n_samples,
            "n_rollouts_per_sample": n_rollouts,
            "bon_count": bon_count,
            "aon_count": aon_count,
            "total_correct": total_correct,
            "total_rollouts": total_rollouts,
        }

        # Save results
        results_file = output_path / "eval_results.json"
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(
                {"metrics": metrics, "detailed_results": results},
                f,
                ensure_ascii=False,
                indent=2,
            )

        logger.info(f"Evaluation complete. Results saved to: {results_file}")
        logger.info(f"BON Score: {metrics['bon_score']:.4f}")
        logger.info(f"AON Score: {metrics['aon_score']:.4f}")
        logger.info(f"AvgN Score: {metrics['avgn_score']:.4f}")

        return metrics


def evaluate_command(args):
    """
    处理train evaluate命令
    """
    try:
        # Setup logging: only output to file, no console output
        logger.remove()
        log_level = "DEBUG" if args.verbose else "INFO"

        # Create log directory under --output-dir so all artifacts of a single
        # evaluation (results + logs) stay in the same folder.
        log_dir = Path(args.output_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"eval_{timestamp}.log"

        # Add file logger only
        logger.add(
            str(log_file),
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            rotation="10 MB",
            encoding="utf-8",
        )
        logger.info(f"评估日志已保存到: {log_file.absolute()}")
        print(f"📝 详细运行日志已保存到: {log_file.absolute()}")

        # Check ARK_API_KEY
        if not os.environ.get("ARK_API_KEY"):
            logger.error("ARK_API_KEY environment variable is not set")
            sys.exit(1)

        # Load dataset
        logger.info(f"Loading dataset from: {args.dataset}")
        dataset = load_dataset(args.dataset)
        logger.info(f"Loaded {len(dataset)} samples")

        # Load rollout function
        logger.info(f"Loading rollout function from: {args.rollout}")
        rollout_func = load_rollout_function(args.rollout)

        # Load grader function
        logger.info(f"Loading grader function from: {args.grader}")
        grader_func = load_grader_function(args.grader)

        # Create config
        # （评估模型由 rollout 内 model= 决定，本命令不再接收 --model。）
        config = EvalConfig(
            n_rollouts=args.n_rollouts,
            max_concurrency=args.max_concurrency,
            batch_size=args.batch_size,
        )

        # Create evaluator and run evaluation
        evaluator = BONEvaluator(config)

        logger.info("Starting evaluation...")
        metrics = asyncio.run(
            evaluator.evaluate(
                dataset=dataset,
                rollout_func=rollout_func,
                grader_func=grader_func,
                output_dir=args.output_dir,
            )
        )

        # Print final results
        print("\n" + "=" * 70)
        print("Evaluation Results")
        print("=" * 70)
        print(f"BON Score:  {metrics['bon_score']:.4f}")
        print(f"AON Score:  {metrics['aon_score']:.4f}")
        print(f"AvgN Score: {metrics['avgn_score']:.4f}")
        print("=" * 70)
        print(f"\nDetailed results saved to: {args.output_dir}")

        return metrics

    except Exception:
        logger.exception("Evaluation failed")
        sys.exit(1)


def _clean_rft_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    清理评估轨迹中的运行时元数据，保留训练需要的对话字段。
    """
    allowed_keys = {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
        "loss_weight",
    }
    cleaned = {
        key: value
        for key, value in message.items()
        if key in allowed_keys and value is not None
    }

    if cleaned.get("role") != "assistant":
        cleaned.pop("reasoning_content", None)
    elif cleaned.get("reasoning_content") == "":
        cleaned.pop("reasoning_content", None)

    if cleaned.get("tool_calls") == []:
        cleaned.pop("tool_calls", None)

    return cleaned


def _messages_use_tools(messages: List[Dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") == "tool" or message.get("tool_calls"):
            return True
    return False


def _normalize_tools_config(raw_config: Any, source: str) -> Dict[str, Any]:
    """
    将 tools 配置规整为 {"tools": [...], "parallel_tool_calls": optional bool}。
    支持直接传 tools 数组，或传包含 tools 字段的对象。
    """
    if raw_config is None:
        return {}

    if isinstance(raw_config, list):
        tools = raw_config
        parallel_tool_calls = None
    elif isinstance(raw_config, dict):
        tools = raw_config.get("tools")
        parallel_tool_calls = raw_config.get("parallel_tool_calls")
    else:
        raise ValueError(f"{source} 的tools配置格式不支持: {type(raw_config).__name__}")

    if not tools:
        return {}

    if not isinstance(tools, list):
        raise ValueError(f"{source} 的tools字段必须是数组")

    config = {"tools": tools}
    if isinstance(parallel_tool_calls, bool):
        config["parallel_tool_calls"] = parallel_tool_calls
    return config


def _load_tools_from_file(tools_file: str) -> Dict[str, Any]:
    path = Path(tools_file)
    if not path.exists():
        raise FileNotFoundError(f"tools文件不存在: {tools_file}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _normalize_tools_config(raw, str(path))
    except json.JSONDecodeError:
        tools = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    tools.append(json.loads(line))
        return _normalize_tools_config(tools, str(path))


def _load_tools_from_rollout(rollout_path: str) -> Dict[str, Any]:
    """
    从 rollout 插件中按约定读取工具定义，支持变量名：
    rollout_tools / tools / TOOL_DEFINITIONS / TOOLS。
    """
    path = Path(rollout_path)
    if not path.exists():
        raise FileNotFoundError(f"Rollout文件不存在: {rollout_path}")

    module_name = f"_ark_trainer_tools_{path.stem}_{abs(hash(path.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载Rollout文件: {rollout_path}")

    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(path.parent))
        except ValueError:
            pass

    tools = None
    for attr_name in ("rollout_tools", "tools", "TOOL_DEFINITIONS", "TOOLS"):
        value = getattr(module, attr_name, None)
        if value:
            tools = value
            break

    config = _normalize_tools_config(tools, rollout_path)
    parallel_tool_calls = getattr(module, "parallel_tool_calls", None)
    if isinstance(parallel_tool_calls, bool):
        config["parallel_tool_calls"] = parallel_tool_calls
    return config


def _resolve_fallback_tools_config(args) -> Dict[str, Any]:
    if getattr(args, "tools_file", None):
        return _load_tools_from_file(args.tools_file)
    if getattr(args, "rollout", None):
        return _load_tools_from_rollout(args.rollout)
    return {}


def _apply_tools_config(sample: Dict[str, Any], tools_config: Dict[str, Any]) -> None:
    if not tools_config:
        return

    if "tools" not in sample:
        sample["tools"] = tools_config["tools"]

    if "parallel_tool_calls" not in sample and "parallel_tool_calls" in tools_config:
        sample["parallel_tool_calls"] = tools_config["parallel_tool_calls"]


def _sample_has_reasoning_content(sample: Dict[str, Any]) -> bool:
    for message in sample.get("messages", []):
        if message.get("reasoning_content"):
            return True
    return False


def _set_thinking_field(sample: Dict[str, Any]) -> None:
    """
    参考数据集Thinking字段处理工具：根据样本是否含reasoning_content设置thinking。
    这里会按拆分后的单条样本重新计算，避免从原始样本继承出
    "thinking enabled 但样本内没有reasoning_content"的不一致格式。
    """
    thinking_type = "enabled" if _sample_has_reasoning_content(sample) else "disabled"
    existing = sample.get("thinking")

    if isinstance(existing, dict):
        updated = existing.copy()
        updated["type"] = thinking_type
        sample["thinking"] = updated
    elif isinstance(existing, str):
        sample["thinking"] = thinking_type
    else:
        sample["thinking"] = {"type": thinking_type}


def _split_multi_turn_reasoning_sample(sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    参考“多轮 reasoning_content 的样本文件拆分”：
    将一条含多轮 reasoning_content 的样本拆成多条仅最后一轮 assistant 携带
    reasoning_content 的样本；继承上下文中的历史 assistant 会去掉 reasoning_content
    并设置 loss_weight=0，避免同一回复被重复训练。
    """
    messages = [_clean_rft_message(msg) for msg in sample.get("messages", [])]
    if not messages:
        return []

    base_sample = {k: v for k, v in sample.items() if k != "messages"}
    message_result_list = []
    processed_messages = []
    not_set_loss_weight_indices = []

    for index, current in enumerate(messages):
        current = current.copy()

        if index == len(messages) - 1:
            if (
                current.get("role") == "assistant"
                and current.get("reasoning_content")
                and not_set_loss_weight_indices
            ):
                for msg_index in not_set_loss_weight_indices:
                    processed_messages[msg_index]["loss_weight"] = 0
                not_set_loss_weight_indices = []
            processed_messages.append(current)
            message_result_list.append([msg.copy() for msg in processed_messages])
            break

        if current.get("role") == "assistant":
            has_loss_weight_zero = current.get("loss_weight") == 0
            has_reasoning_content = bool(current.get("reasoning_content"))

            if not has_loss_weight_zero and has_reasoning_content:
                current_segment = [msg.copy() for msg in processed_messages]
                current_segment.append(current.copy())
                message_result_list.append(current_segment)

                continuing_message = current.copy()
                continuing_message.pop("reasoning_content", None)
                continuing_message["loss_weight"] = 0
                processed_messages.append(continuing_message)

                for msg_index in not_set_loss_weight_indices:
                    processed_messages[msg_index]["loss_weight"] = 0
                not_set_loss_weight_indices = []

            elif has_loss_weight_zero:
                continuing_message = current.copy()
                continuing_message.pop("reasoning_content", None)
                processed_messages.append(continuing_message)
            else:
                processed_messages.append(current)
                if "loss_weight" not in current:
                    not_set_loss_weight_indices.append(len(processed_messages) - 1)
        else:
            processed_messages.append(current)

    processed_samples = []
    for messages_part in message_result_list:
        processed_sample = base_sample.copy()
        processed_sample["messages"] = messages_part
        _set_thinking_field(processed_sample)
        processed_samples.append(processed_sample)

    return processed_samples


def rft_data_collect_command(args):
    """
    处理train rft-data-collect命令
    RFT数据收集：从评估结果中筛选reward=1.0的优质轨迹，生成RFT训练数据
    """
    try:
        print("\n=== 收集RFT训练数据 ===")

        input_path = Path(args.eval_results)
        if not input_path.exists():
            raise FileNotFoundError(f"评估结果文件不存在: {args.eval_results}")

        # 
        with open(input_path, "r", encoding="utf-8") as f:
            eval_data = json.load(f)

        detailed_results = eval_data.get("detailed_results", [])
        if not detailed_results:
            logger.error("评估结果中没有detailed_results数据")
            return None

        fallback_tools_config = _resolve_fallback_tools_config(args)
        if fallback_tools_config:
            logger.info(
                f"已加载tools定义: {len(fallback_tools_config.get('tools', []))}个"
            )

        rft_samples = []
        total_correct = 0
        missing_tools_count = 0

        for sample in detailed_results:
            data = sample.get("data", {})

            # reward=1
            for rollout in sample.get("rollouts", []):
                if rollout.get("correct", False) and rollout.get("reward", 0) == 1.0:
                    trajectory = rollout.get("trajectory", {})
                    if not isinstance(trajectory, dict):
                        logger.warning(
                            f"跳过格式异常的trajectory: sample_idx={sample.get('sample_idx')}"
                        )
                        continue

                    traj_messages = trajectory.get("messages", [])

                    if not traj_messages:
                        continue

                    # RFT：prompt 沿用 data 字段，messages 用完整轨迹。
                    # Function Calling / RL 场景保留 assistant.tool_calls 与 tool 消息。
                    rft_sample = {k: v for k, v in data.items() if k != "messages"}
                    rft_sample["messages"] = traj_messages
                    trajectory_tools_config = _normalize_tools_config(
                        trajectory, "trajectory"
                    )
                    if "tools" not in rft_sample and trajectory_tools_config:
                        _apply_tools_config(rft_sample, trajectory_tools_config)

                    if "tools" not in rft_sample and _messages_use_tools(traj_messages):
                        if fallback_tools_config:
                            _apply_tools_config(rft_sample, fallback_tools_config)
                        else:
                            missing_tools_count += 1

                    rft_samples.extend(_split_multi_turn_reasoning_sample(rft_sample))
                    total_correct += 1
                    break  # 每个样本只取第一个正确的

        # RFT
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            for sample in rft_samples:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        print("✅ RFT数据收集完成！")
        print(f"总样本数: {len(detailed_results)}")
        print(f"正确样本数: {total_correct}")
        print(f"输出样本数: {len(rft_samples)}")
        if fallback_tools_config:
            print(f"已补充tools定义: {len(fallback_tools_config.get('tools', []))}个")
        if missing_tools_count:
            print(
                f"⚠️  有 {missing_tools_count} 条含 tool_calls/tool 消息的轨迹缺少顶层 tools。请通过 --rollout 或 --tools-file 补充工具定义。"
            )
        print(f"RFT训练数据已保存到: {output_path}")

        return rft_samples

    except Exception as e:
        logger.exception(f"RFT数据收集失败: {str(e)}")
        return None
