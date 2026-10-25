from __future__ import annotations

"""
火山云手机 CLI 工具
"""

import argparse
import sys

from core import (
    apps,
    cli_common,
    dcs,
    display_layouts,
    generic,
    hosts,
    images,
    instance_controls,
    instance_properties,
    instances,
    network,
    products,
    resources,
    tags,
    tasks,
)

CLI_VERSION = "1.1.0"


def _extract_global_config_arg(argv):
    remaining = []
    config_path = None
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--config":
            if i + 1 >= len(argv):
                raise SystemExit("错误: --config 需要传入文件路径")
            config_path = argv[i + 1]
            i += 2
            continue
        if item.startswith("--config="):
            config_path = item.split("=", 1)[1]
            if not config_path:
                raise SystemExit("错误: --config 需要传入文件路径")
            i += 1
            continue
        remaining.append(item)
        i += 1
    return remaining, config_path


COMMAND_GROUPS = (
    ("common", "Generic action calls and core capabilities", (generic,)),
    ("task", "Task queries and troubleshooting", (tasks,)),
    ("instance", "Instance lifecycle and attributes", (instances, instance_properties)),
    (
        "hosts-images",
        "Hosts, images, and datacenter queries",
        (hosts, images, dcs, products, resources, display_layouts),
    ),
    ("app", "App upload, install, launch and query", (apps,)),
    ("device-control", "Screen capture, screenshots, files and command execution", (instance_controls,)),
    ("network", "Tags, DNS, routes and port mapping", (tags, network)),
)


def _register_command_groups(subparsers):
    groups = []
    for title, summary, modules in COMMAND_GROUPS:
        before = set(subparsers.choices)
        for module in modules:
            module.register(subparsers)
        commands = sorted(set(subparsers.choices) - before)
        groups.append(
            {
                "title": title,
                "summary": summary,
                "commands": commands,
            }
        )
    return groups


def _command_help_map(subparsers):
    return {action.dest: action.help or "" for action in subparsers._get_subactions()}


def _format_command_line(command, help_text):
    return f"    {command:<28} {help_text}".rstrip()


def print_top_level_help(parser, subparsers, groups):
    help_map = _command_help_map(subparsers)
    lines = [
        parser.description,
        "",
        "用法:",
        "  vephone <command> [args]",
        "  vephone <command> -h",
        "",
        "说明:",
        "  大多数业务命令都需要显式传入 product_id。",
        "  通过 --config 指定 config.json 文件路径。",
        "  查看某个命令的详细参数，请使用 vephone <command> -h。",
        "",
        "命令分组:",
    ]

    for group in groups:
        lines.append(f"  {group['title']}  {group['summary']}")
        for command in group["commands"]:
            lines.append(_format_command_line(command, help_map.get(command, "")))
        lines.append("")

    lines.extend(
        [
            "示例:",
            "  vephone list-products --count 10",
            "  vephone action-call ListOperableProduct --json-body --param Count=10",
            "  vephone list-pods <product_id> --max-results 10",
            "  vephone detail-pod <product_id> <pod_id>",
            "  vephone get-task-info <product_id> <task_id>",
        ]
    )
    print("\n".join(lines).rstrip())


def build_parser():
    parser = argparse.ArgumentParser(
        prog="vephone",
        description="火山云手机 CLI 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="指定 config.json 文件路径")
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {CLI_VERSION}"
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", help="命令名称"
    )
    groups = _register_command_groups(subparsers)
    return parser, subparsers, groups


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, config_path = _extract_global_config_arg(argv)
    cli_common.set_config_path(config_path)
    parser, subparsers, groups = build_parser()

    if not argv or argv == ["-h"] or argv == ["--help"]:
        print_top_level_help(parser, subparsers, groups)
        sys.exit(0 if argv else 1)

    args = parser.parse_args(argv)
    if not args.command:
        print_top_level_help(parser, subparsers, groups)
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
