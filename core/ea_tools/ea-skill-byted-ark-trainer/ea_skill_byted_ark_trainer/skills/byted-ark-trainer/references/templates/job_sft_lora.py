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
SFT LoRA training job submission template (FinetuneLoRA).

Usage instructions:
1. Copy this file to your experiment subdirectory (experiments/exp_xxx/job.py),
   then adjust the values to match your specific setup.
2. The ``hyperparameters`` field MUST only contain keys that appear in the
   query output of ``ark get foundation-model --model <X> --version <Y>
   --fields hyperparameters``. Fields not listed in the query output are
   not permitted.
3. Submit the job by running ``python job.py`` from within the experiment
   subdirectory.

Common pitfalls:
- ``model_version`` must be a string (e.g. "250615"), never an integer.
- ``data.training_set`` must be a TrainingDataset object containing at least
  one of ``local_files``, ``tos_bucket``, or ``datasets``.
- SFT jobs must never include ``custom_rl_pipeline`` or ``enable_trajectory``.
- The SFT learning rate field is ``learning_rate``, not ``lr``. These two
  field names are not interchangeable.
- Do not add internal fields such as ``dyn_bsz`` or ``freeze_vit`` from memory.
"""

import sys
import os

# Add working directory to Python path so the job can import local modules
sys.path.insert(0, os.getcwd())

from ark_sdk.resources.model_customization_job import ModelCustomizationJob
from ark_sdk.types.model_customization_job.model_customization_job import (
    CustomizationType,
)


if __name__ == "__main__":
    mcj = ModelCustomizationJob(
        name="sft-lora-demo",
        model_reference={
            "foundation_model": {
                "name": "doubao-seed-1-6",
                "model_version": "250615",  # Must be a string, not an integer
            }
        },
        customization_type=CustomizationType.FinetuneLoRA,
        hyperparameters={
            # Query reference: ark get foundation-model ... --fields hyperparameters
            # under the FinetuneLoRA section.
            "epoch": "1",
            "batch_size": "8",
            "learning_rate": "0.00001",  # SFT uses 'learning_rate', not 'lr'
            "warmup_step_rate": "0.05",
            "seq_len": "32768",
            "lora_rank": "32",
            "lora_alpha": "4",
            "save_model_per_epoch": "1",
        },
        data={
            "training_set": {
                "local_files": [
                    "./data/sft_train_data.jsonl",
                ],
            },
            # Optional: validation set for periodic evaluation during training
            "validation_percentage": 10,
            # "validation_set": {"local_files": ["./data/sft_val_data.jsonl"]},
        },
        save_model_limit=1,
    )

    mcj.submit()
    print(f"Job submitted. view job at {mcj.url}")
