#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre Agent.
# The Encre Agent project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DISCLAIMER: Users must comply with applicable AI regulations.
# Non-compliance may result in service termination or legal liability.

from __future__ import annotations

"""
GRPO LoRA training job submission template (GRPOLoRA).

Usage instructions:
1. Copy this file to your experiment subdirectory (experiments/exp_xxx/job.py),
   then adjust the values to match your specific setup.
2. The ``hyperparameters`` field MUST only contain keys that appear in the
   ``GRPOLoRA`` section returned by ``ark get foundation-model --model <X>
   --version <Y> --fields hyperparameters``. Fields not listed in the query
   output are not permitted.
3. Submit the job by running ``python job.py`` from within the experiment
   subdirectory.
4. ``enable_trajectory`` is strongly recommended (set to True) for
   trajectory analysis in the console. This requires the TLS log service
   to be pre-activated.

Common pitfalls:
- ``model_version`` must be a string (e.g. "250615"), never an integer.
- The GRPO learning rate field is ``lr``, not ``learning_rate``. Do not
  reuse SFT field names.
- ``num_generations`` must be one of the discrete values returned by the
  query (typically {8, 16, 32}).
- GRPO requires ``custom_rl_pipeline``. SFT must never include this field.
- Do not add internal fields such as ``loss_name`` from memory.
"""

import sys
import os

# Add working directory to Python path so plugins can be imported
sys.path.insert(0, os.getcwd())

from ark_sdk.resources.model_customization_job import ModelCustomizationJob
from ark_sdk.resources.pipeline_plugin import PipelinePluginWrapper
from ark_sdk.resources.pipeline_plugin.pipeline_plugin import GRPOPipeline
from ark_sdk.types.model_customization_job.model_customization_job import (
    CustomizationType,
)

# Rollout and grader plugin imports for the RL training pipeline
from plugins.random_reward import random_reward_fn
from plugins.weather_rollout import demo_rollout


if __name__ == "__main__":
    mcj = ModelCustomizationJob(
        name="grpo-lora-demo",
        # model_reference: specifies which base or previously fine-tuned model
        # to build the GRPO training on top of.
        model_reference={
            # (a) Foundation model option (use when starting from a base model):
            # "foundation_model": {
            #     "name": "doubao-seed-1-6-flash",
            #      "model_version": "250615",  # String, e.g. "250615"
            # },
            # (b) RFT/SFT (GRPO) option (use when fine-tuning an existing custom model):
            "custom_model_id": "cm-xxxxxxxxxxxxxx-xxxxx",
        },
        customization_type=CustomizationType.GRPOLoRA,
        hyperparameters={
            # Query reference: ark get foundation-model ... --fields hyperparameters
            # under the GRPOLoRA section.
            "num_steps": "20",
            "batch_size": "32",  # GRPOLoRA batch_size is an enum; minimum is 32
            "lr": "0.000001",  # GRPO uses 'lr', not 'learning_rate'
            "lr_warmup_steps": "5",
            "num_generations": "8",
            "num_iterations_per_batch": "2",
            "temperature": "1.0",
            "top_p": "1",
            "max_new_tokens": "1024",
            "clip_ratio_high": "0.2",
            "clip_ratio_low": "0.2",
            "kl_coefficient": "0.001",
            "loss_agg_mode": "seq-mean-token-mean",
            "save_every_n_steps": "10",
            "test_every_n_steps": "5",
            "test_num_generations": "1",
            "test_top_p": "1",
            "lora_rank": "32",
            "lora_alpha": "4",
        },
        data={
            "training_set": {
                "local_files": [
                    "./data/rl_train_data.jsonl",
                ],
            },
            # Optional: validation set for periodic evaluation during training
            # "validation_set": {"local_files": ["./data/rl_test_data.jsonl"]},
        },
        custom_rl_pipeline=GRPOPipeline(
            graders=[
                PipelinePluginWrapper(
                    plugin=random_reward_fn,
                    envs={"foo": "bar"},
                    weight=0.5,
                ),
            ],
            rollout=PipelinePluginWrapper(
                plugin=demo_rollout,
                envs={"foo": "bar"},
            ),
        ),
        enable_trajectory=True,  # Recommended for RL training (requires TLS log service)
        save_model_limit=1,
    )

    mcj.submit()
    print(f"Job submitted. view job at {mcj.url}")
