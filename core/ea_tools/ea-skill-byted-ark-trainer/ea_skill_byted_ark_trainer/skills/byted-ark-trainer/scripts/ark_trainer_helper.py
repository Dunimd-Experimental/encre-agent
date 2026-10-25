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
ARK Trainer Helper CLI Tool

A command-line utility for ARK training operations including:

- Training Job Management:
    - Query training job status
    - Retrieve output model ID from a completed job
    - Register a job to the workspace HEARTBEAT.md file
- Foundation Model Management:
    - List foundation models (supports name fuzzy search and training type filter)
    - List all available versions of a foundation model
- Endpoint Management:
    - Create, list, get, get certificate, stop, and delete inference endpoints
- Training Utilities:
    - Model evaluation (compute BON/AON/AvgN metrics)
    - RFT training data collection

Usage examples::

    # Query training job status
    ark-trainer-helper job status --job-id mcj-20260225163459-thh2g

    # Get output model ID from a completed job
    ark-trainer-helper job get-model --job-id mcj-20260225163459-thh2g

    # List foundation models (filter by name 'doubao' and type FinetuneLoRA)
    ark-trainer-helper model list-models --name doubao --supported-customization-type FinetuneLoRA

    # List available versions of a foundation model
    ark-trainer-helper model list-versions --model-name doubao-seed-1-6

    # Create an inference endpoint
    ark-trainer-helper endpoint create --name my-endpoint --description "Test endpoint" --custom-model-id cm-123456

    # List all endpoints
    ark-trainer-helper endpoint list

    # Run model evaluation (the model under evaluation is determined by the
    # 'model=' field inside rollout.py, not by this command)
    ark-trainer-helper train evaluate --dataset test.jsonl --rollout rollout.py --grader grader.py

    # Collect RFT training data from evaluation results
    ark-trainer-helper train rft-data-collect --eval-results ./eval_output/eval_results.json --output-file ./rft_data.jsonl --rollout rollout.py
"""

import argparse
import os

# Import command implementations from domain-specific modules
from modules.job import (
    job_status_command,
    job_get_model_command,
    register_heartbeat_command,
)
from modules.model import list_foundation_model_versions, list_foundation_models
from modules.endpoint import (
    init_api_client,
    create_endpoint,
    list_endpoints,
    get_endpoint,
    get_endpoint_certificate,
    stop_endpoint,
    delete_endpoint,
)
from modules.train import evaluate_command, rft_data_collect_command


def main():
    """
    Main entry point for the ARK Trainer Helper CLI.

    Builds and configures a hierarchical ``argparse`` parser with four
    top-level subcommands (job, model, endpoint, train), each containing
    its own set of sub-subcommands. After parsing, dispatches to the
    corresponding command handler based on the selected module and action.

    The parser uses ``RawDescriptionHelpFormatter`` so that multi-line
    epilog examples are preserved exactly as written, without automatic
    reflow.
    """
    # Create the top-level argument parser with metadata and help formatting
    parser = argparse.ArgumentParser(
        prog="ark-trainer-helper",
        description="ARK Trainer Helper CLI Tool",
        epilog="Example: ark-trainer-helper job status --job-id mcj-20260225163459-thh2g",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Register the four top-level module subcommands
    subparsers = parser.add_subparsers(
        dest="module", title="Available Modules", description="Select a module to operate on", help="Module help"
    )

    # Shared global parser for options that apply across multiple subcommands
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument(
        "--project-name", default="default", help="Project name (default: default)"
    )

    # ==================== job module =======================

    job_parser = subparsers.add_parser("job", help="Training job management")
    job_subparsers = job_parser.add_subparsers(
        dest="job_command", title="Training Job Commands", help="Training job operation commands"
    )

    # job status -- query the current state of a training job
    job_status_parser = job_subparsers.add_parser("status", help="Query training job status")
    job_status_parser.add_argument(
        "--job-id", required=True, help="Training job ID (e.g. mcj-20260225163459-thh2g)"
    )

    # job get-model -- retrieve the output custom model ID after training completes
    job_get_model_parser = job_subparsers.add_parser(
        "get-model", help="Get the model ID produced by a completed training job"
    )
    job_get_model_parser.add_argument(
        "--job-id", required=True, help="Training job ID (e.g. mcj-20260225163459-thh2g)"
    )

    # job register-heartbeat -- register a training job in the workspace HEARTBEAT.md file
    register_hb_parser = job_subparsers.add_parser(
        "register-heartbeat",
        help="Register a training job in the workspace HEARTBEAT.md (auto-maintains the top system reminder block)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register an SFT job to HEARTBEAT.md
  ark-trainer-helper job register-heartbeat \\
      --job-id mcj-20260425143200-sft01 \\
      --job-type SFT \\
      --job-url https://console.volcengine.com/ark/region:ark+cn-beijing/finetune/detail?Id=mcj-20260425143200-sft01 \\
      --exp-dir /abs/path/workspace/experiments/exp_20260425_143200_sft_lora

Behavior:
  - If HEARTBEAT.md does not exist, create it from the full template (6 AI system reminders + table header + new task row).
  - If the file exists but the top system reminder block is missing or incomplete, prepend the complete reminder block.
  - If the same --job-id is already registered, skip idempotently.
        """,
    )
    register_hb_parser.add_argument(
        "--job-id", required=True, help="Training job ID (e.g. mcj-20260425143200-sft01)"
    )
    register_hb_parser.add_argument(
        "--job-type",
        required=True,
        help="Job type label for heartbeat table display (e.g. SFT / RFT / GRPO / RFT+GRPO)",
    )
    register_hb_parser.add_argument(
        "--job-url", required=True, help="Console detail page URL for the job"
    )
    register_hb_parser.add_argument(
        "--exp-dir",
        required=True,
        help="Absolute path to the experiment subdirectory (e.g. /abs/path/workspace/experiments/exp_xxx)",
    )
    register_hb_parser.add_argument(
        "--submit-time",
        default=None,
        help="Job submission time string, defaults to current time (recommended format: 'YYYY-MM-DD HH:MM')",
    )
    register_hb_parser.add_argument(
        "--status",
        default="Running",
        help="Initial status literal to write to the table (default: Running)",
    )
    register_hb_parser.add_argument(
        "--heartbeat-file",
        default=os.path.expanduser("~/.openclaw/workspace/HEARTBEAT.md"),
        help=(
            "Path to HEARTBEAT.md, defaults to ~/.openclaw/workspace/HEARTBEAT.md. "
            "OpenClaw only triggers heartbeats under this path; it must be this directory "
            "or a subdirectory containing HEARTBEAT.md."
        ),
    )

    # ==================== model module =======================

    model_parser = subparsers.add_parser("model", help="Foundation model management")
    model_subparsers = model_parser.add_subparsers(
        dest="model_command", title="Foundation Model Commands", help="Foundation model operation commands"
    )

    # model list-models -- query foundation models with optional name and type filters
    model_list_parser = model_subparsers.add_parser(
        "list-models",
        help="Query foundation models; supports name fuzzy search and training type filter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all LLM foundation models
  ark-trainer-helper model list-models

  # Filter by name containing 'doubao'
  ark-trainer-helper model list-models --name doubao

  # Query models supporting FinetuneLoRA training
  ark-trainer-helper model list-models --supported-customization-type FinetuneLoRA

  # Combine filters (name 'doubao' + GRPOLoRA)
  ark-trainer-helper model list-models --name doubao --supported-customization-type GRPOLoRA
        """,
    )
    model_list_parser.add_argument("--name", help="Model name fuzzy search keyword")
    model_list_parser.add_argument(
        "--supported-customization-type",
        help="Supported training type filter (e.g. FinetuneLoRA, GRPOLoRA, FinetuneSft, GRPO)",
    )
    model_list_parser.add_argument(
        "--page-size", type=int, default=10, help="Items per page (default: 10)"
    )
    model_list_parser.add_argument(
        "--page-number", type=int, default=1, help="Page number (default: 1)"
    )

    # model list-versions -- list all available versions of a specific foundation model
    model_list_versions_parser = model_subparsers.add_parser(
        "list-versions", help="List all available versions of a foundation model"
    )
    model_list_versions_parser.add_argument(
        "--model-name", required=True, help="Foundation model name (e.g. doubao-seed-1-6)"
    )
    model_list_versions_parser.add_argument(
        "--page-size", type=int, default=50, help="Items per page (default: 50)"
    )
    model_list_versions_parser.add_argument(
        "--page-number", type=int, default=1, help="Page number (default: 1)"
    )

    # ==================== endpoint module =======================

    endpoint_parser = subparsers.add_parser("endpoint", help="Inference endpoint management")
    endpoint_subparsers = endpoint_parser.add_subparsers(
        dest="endpoint_command", title="Endpoint Commands", help="Endpoint operation commands"
    )

    # endpoint create -- create a new inference endpoint for a custom model
    create_parser = endpoint_subparsers.add_parser(
        "create", parents=[global_parser], help="Create a new endpoint"
    )
    create_parser.add_argument("--name", required=True, help="Endpoint name")
    create_parser.add_argument("--description", default="", help="Endpoint description")
    create_parser.add_argument("--custom-model-id", required=True, help="Custom model ID to deploy")

    # endpoint list -- list all endpoints with pagination
    list_parser = endpoint_subparsers.add_parser(
        "list", parents=[global_parser], help="List all endpoints"
    )
    list_parser.add_argument(
        "--page-size", type=int, default=10, help="Items per page (default: 10)"
    )
    list_parser.add_argument(
        "--page-number", type=int, default=1, help="Page number (default: 1)"
    )

    # endpoint get -- retrieve detailed information about a single endpoint
    get_parser = endpoint_subparsers.add_parser("get", help="Get endpoint details")
    get_parser.add_argument("--endpoint-id", required=True, help="Endpoint ID")

    # endpoint certificate -- fetch the TLS certificate associated with an endpoint
    certificate_parser = endpoint_subparsers.add_parser(
        "certificate", help="Get endpoint TLS certificate"
    )
    certificate_parser.add_argument("--endpoint-id", required=True, help="Endpoint ID")

    # endpoint stop -- gracefully stop a running endpoint
    stop_parser = endpoint_subparsers.add_parser("stop", help="Stop an endpoint")
    stop_parser.add_argument("--endpoint-id", required=True, help="Endpoint ID")

    # endpoint delete -- permanently delete an endpoint
    delete_parser = endpoint_subparsers.add_parser("delete", help="Delete an endpoint")
    delete_parser.add_argument("--endpoint-id", required=True, help="Endpoint ID")

    # ==================== train module =======================

    train_parser = subparsers.add_parser("train", help="Training utility toolkit")
    train_subparsers = train_parser.add_subparsers(
        dest="train_command", title="Training Utility Commands", help="Training-related utility commands"
    )

    # train evaluate -- run BON/AON/AvgN evaluation on a model using rollout and grader plugins
    evaluate_parser = train_subparsers.add_parser(
        "evaluate",
        help="Model evaluation (compute BON/AON/AvgN metrics)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
IMPORTANT: The model being evaluated is determined by the rollout.py file itself -- specifically the
'model=' string passed to chat.completions.create() inside that file. This command does NOT accept
a --model argument. Before running evaluate, manually edit rollout.py to set the target model name,
version, endpoint ID, or custom model ID, then run this command.

Examples:
  # Basic evaluation
  ark-trainer-helper train evaluate --dataset test.jsonl --rollout rollout.py --grader grader.py

  # With concurrency tuning
  ark-trainer-helper train evaluate --dataset test.jsonl --rollout rollout.py --grader grader.py \\
                     --n-rollouts 8 --max-concurrency 15

  # Custom output directory
  ark-trainer-helper train evaluate --dataset test.jsonl --rollout rollout.py --grader grader.py \\
                     --output-dir ./my_results
        """,
    )
    evaluate_parser.add_argument(
        "--dataset", type=str, required=True, help="Evaluation dataset path (JSON or JSONL format)"
    )
    evaluate_parser.add_argument(
        "--rollout",
        type=str,
        required=True,
        help="Rollout function Python file path (the 'model=' field inside determines which model is evaluated)",
    )
    evaluate_parser.add_argument(
        "--grader", type=str, required=True, help="Grader function Python file path"
    )
    evaluate_parser.add_argument(
        "--n-rollouts", type=int, default=8, help="Number of rollouts per sample (default: 8)"
    )
    evaluate_parser.add_argument(
        "--batch-size", type=int, default=15, help="Evaluation batch size (default: 15)"
    )
    evaluate_parser.add_argument(
        "--max-concurrency", type=int, default=15, help="Maximum concurrent rollouts (default: 15)"
    )
    evaluate_parser.add_argument(
        "--output-dir",
        type=str,
        default="./eval_output",
        help="Evaluation results output directory (default: ./eval_output)",
    )
    evaluate_parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")

    # train rft-data-collect -- filter high-quality trajectories from evaluation results to produce RFT training data
    rft_data_parser = train_subparsers.add_parser(
        "rft-data-collect",
        help="RFT training data collection: filter high-quality trajectories from evaluation results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Collect RFT data with rollout-based tool inference
  ark-trainer-helper train rft-data-collect --eval-results ./eval_output/eval_results.json \\
                                            --output-file ./rft_train_data.jsonl \\
                                            --rollout ./rollout.py

  # Collect RFT data with explicit tools file (JSON array or JSON object with 'tools' field)
  ark-trainer-helper train rft-data-collect --eval-results ./eval_output/eval_results.json \\
                                            --output-file ./rft_train_data.jsonl \\
                                            --tools-file ./tools.json
        """,
    )
    rft_data_parser.add_argument(
        "--eval-results",
        type=str,
        required=True,
        help="Evaluation results JSON file path (produced by the evaluate command)",
    )
    rft_data_parser.add_argument(
        "--output-file",
        type=str,
        required=True,
        help="Output RFT training data file path (JSONL format)",
    )
    rft_data_parser.add_argument(
        "--rollout",
        type=str,
        help="Optional: rollout plugin path; used to extract the top-level 'tools' field required by Function Calling samples",
    )
    rft_data_parser.add_argument(
        "--tools-file",
        type=str,
        help="Optional: tools definition file path (JSON array or JSON object with 'tools' field); takes priority over --rollout",
    )

    # Parse command-line arguments into a Namespace object
    args = parser.parse_args()

    # If no module subcommand was selected, show the top-level help and exit
    if not args.module:
        parser.print_help()
        return

    # --- job module dispatch ---
    if args.module == "job":
        if not args.job_command:
            job_parser.print_help()
            return

        if args.job_command == "status":
            job_status_command(args)
        elif args.job_command == "get-model":
            job_get_model_command(args)
        elif args.job_command == "register-heartbeat":
            register_heartbeat_command(args)

    # --- model module dispatch ---
    elif args.module == "model":
        if not args.model_command:
            model_parser.print_help()
            return

        if args.model_command == "list-models":
            list_foundation_models(args)
        elif args.model_command == "list-versions":
            list_foundation_model_versions(args)

    # --- endpoint module dispatch ---
    elif args.module == "endpoint":
        if not args.endpoint_command:
            endpoint_parser.print_help()
            return

        # Initialize the ARK API client with credentials from environment variables
        api = init_api_client()
        if not api:
            return

        # Execute the selected endpoint operation
        if args.endpoint_command == "create":
            create_endpoint(api, args)
        elif args.endpoint_command == "list":
            list_endpoints(api, args)
        elif args.endpoint_command == "get":
            get_endpoint(api, args)
        elif args.endpoint_command == "certificate":
            get_endpoint_certificate(api, args)
        elif args.endpoint_command == "stop":
            stop_endpoint(api, args)
        elif args.endpoint_command == "delete":
            delete_endpoint(api, args)

    # --- train module dispatch ---
    elif args.module == "train":
        if not args.train_command:
            train_parser.print_help()
            return

        if args.train_command == "evaluate":
            evaluate_command(args)
        elif args.train_command == "rft-data-collect":
            rft_data_collect_command(args)


if __name__ == "__main__":
    main()
