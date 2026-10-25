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

"""Application (app lifecycle) command module for the ACEP cloud-phone API.

This module exposes one ``cmd_<action>`` function per app-lifecycle
operation (install, launch, close, uninstall, upload, delete, etc.) and
a single ``register()`` that wires each command into the CLI's argparse
sub-parser tree.  It deliberately keeps the thin-CLI / thin-handler
shape: each ``cmd_*`` function resolves its client through
``cli.get_client()``, forwards the call, and lets
``cli.print_result()`` handle JSON serialisation and error reporting.

Because the underlying REST API is stateless per request, there is no
caching or retry logic at this layer — the transport layer
(``vephone_client``) owns retries and auth-refresh behaviour.
"""
from . import cli_common as cli


def parse_app_list(value: str) -> list[dict]:
    """Parse a comma-separated ``AppId:VersionId`` list into request dicts.

    Each item is expected in the form ``AppId:VersionId``; anything
    missing a colon or an empty side raises ``ValueError`` early so the
    caller sees a clean argparse-style error rather than a 400 from the
    API.  The returned list is passed directly to ``install_apps``.

    Args:
        value: Raw CLI value, e.g. ``"a1:v1,a2:v2"``.

    Returns:
        List of ``{'AppId': ..., 'VersionId': ...}`` dicts.

    Raises:
        ValueError: If any item is malformed (missing colon or empty part).
    """
    result = []
    for item in cli.parse_csv_values(value):
        app_id, sep, version_id = item.partition(':')
        if not app_id or not sep or not version_id:
            raise ValueError(f'Invalid --app-list item: {item!r}; expected AppId:VersionId')
        result.append({'AppId': app_id, 'VersionId': version_id})
    return result


def cmd_install_app(args):
    """Install a single app version onto one instance."""
    cli.print_result(cli.get_client().install_app(
        pod_id=args.pod_id,
        app_id=args.app_id,
        version_id=args.version_id,
        **cli.request_kwargs(args),
    ))


def cmd_launch_app(args):
    """Launch an app identified by its package name on one instance."""
    cli.print_result(cli.get_client().launch_app(
        pod_id=args.pod_id,
        package_name=args.package_name,
        **cli.request_kwargs(args),
    ))


def cmd_close_app(args):
    """Force-close a running app on one instance."""
    cli.print_result(cli.get_client().close_app(
        pod_id=args.pod_id,
        package_name=args.package_name,
        **cli.request_kwargs(args),
    ))


def cmd_uninstall_app(args):
    """Uninstall a previously installed app from one instance."""
    cli.print_result(cli.get_client().uninstall_app(
        pod_id=args.pod_id,
        app_id=args.app_id,
        **cli.request_kwargs(args),
    ))


def cmd_auto_install_app(args):
    """Install apps automatically onto multiple instances in one shot.

    Optional fields are collected from the CLI and forwarded to the API;
    the ``install_type`` / ``download_url`` / ``package_name`` /
    ``version_code`` / ``image_id`` / ``absolute_path`` fields are only
    included when the user actually supplies them, so the API can decide
    which installation mode to use.  ``is_preinstall`` is a tri-state
    flag: ``None`` (unspecified), ``True`` (explicitly set), and it is
    passed as ``IsPreinstall`` to the API when present.
    """
    kwargs = cli.request_kwargs(args)
    for field in ['install_type', 'download_url', 'package_name', 'version_code', 'image_id', 'absolute_path']:
        value = getattr(args, field, None)
        if value is not None:
            kwargs[field] = value
    if args.is_preinstall is not None:
        kwargs['IsPreinstall'] = args.is_preinstall
    cli.print_result(cli.get_client().auto_install_app(
        pod_id_list=cli.parse_csv_values(args.pod_id_list),
        **kwargs,
    ))


def cmd_get_pod_app_list(args):
    """List all apps currently installed on a single instance."""
    cli.print_result(cli.get_client().get_pod_app_list(args.pod_id, **cli.request_kwargs(args)))


def cmd_detail_app(args):
    """Fetch detailed metadata for a single app."""
    cli.print_result(cli.get_client().detail_app(app_id=args.app_id, product_id=args.product_id))


def cmd_list_apps(args):
    """List apps with pagination and optional filters."""
    cli.print_result(cli.get_client().list_apps(
        max_results=args.max_results,
        next_token=args.next_token,
        product_id=args.product_id,
        AppId=args.app_id,
        AppName=args.app_name,
        AppType=args.app_type,
        PackageNameList=cli.parse_csv_values(args.package_name_list),
    ))


def cmd_list_app_version_deploys(args):
    """List deploy records for every version of a given app."""
    cli.print_result(cli.get_client().list_app_version_deploys(
        app_id=args.app_id,
        product_id=args.product_id,
        VersionId=args.version_id,
    ))


def cmd_get_app_crash_log(args):
    """Retrieve crash logs for the given instances within a time window.

    Both start and end times are Unix-epoch seconds; the API requires
    both to be present, so the CLI makes them ``required=True``.
    """
    cli.print_result(cli.get_client().get_app_crash_log(
        pod_id_list=args.pod_id_list,
        start_time=args.start_time,
        end_time=args.end_time,
        product_id=args.product_id,
    ))


def cmd_install_apps(args):
    """Batch-install multiple app versions onto a single instance.

    ``app_list`` is a comma-separated string of ``AppId:VersionId``
    pairs parsed by ``parse_app_list``; malformed items raise
    ``ValueError`` before any API call is made.
    """
    cli.print_result(cli.get_client().install_apps(
        pod_id=args.pod_id,
        app_list=parse_app_list(args.app_list),
        product_id=args.product_id,
        install_type=args.install_type,
        is_preinstall=args.is_preinstall,
    ))


def cmd_upload_app(args):
    """Upload a new app artifact to the ACEP app registry."""
    cli.print_result(cli.get_client().upload_app(
        app_type=args.app_type,
        download_url=args.download_url,
        product_id=args.product_id,
        app_id=args.app_id,
        app_name=args.app_name,
        rotation=args.rotation,
        app_desc=args.app_desc,
        parse_flag=args.parse_flag,
        app_mode=args.app_mode,
    ))


def cmd_update_app(args):
    """Update metadata on an existing app (name, icon, description, etc.)."""
    cli.print_result(cli.get_client().update_app(
        app_id=args.app_id,
        product_id=args.product_id,
        app_name=args.app_name,
        rotation=args.rotation,
        icon_url=args.icon_url,
        app_desc=args.app_desc,
        app_mode=args.app_mode,
    ))


def cmd_delete_app(args):
    """Delete an app and all of its versions from the registry."""
    cli.print_result(cli.get_client().delete_app(app_id=args.app_id, product_id=args.product_id))


def cmd_delete_app_version(args):
    """Delete a single version of an app; the parent app remains intact."""
    cli.print_result(cli.get_client().delete_app_version(version_id=args.version_id, product_id=args.product_id))


def cmd_launch_apps(args):
    """Launch multiple apps (by package name) on a single instance."""
    cli.print_result(cli.get_client().launch_apps(
        pod_id=args.pod_id,
        package_name_list=cli.parse_csv_values(args.package_name_list),
        product_id=args.product_id,
    ))


def register(subparsers):
    """Register all app-lifecycle sub-commands with the top-level parser.

    Each ``add_parser`` call binds a command name to its ``cmd_*``
    handler via ``set_defaults(func=...)``.  Help strings are kept short
    so they stay readable in the CLI's ``--help`` output.
    """

    install_app_parser = subparsers.add_parser('install-app', help='Install an application')
    install_app_parser.add_argument('product_id', help='Product ID')
    install_app_parser.add_argument('pod_id', help='Instance ID')
    install_app_parser.add_argument('app_id', help='Application ID')
    install_app_parser.add_argument('version_id', help='Application version ID')
    install_app_parser.set_defaults(func=cmd_install_app)

    launch_app_parser = subparsers.add_parser('launch-app', help='Launch an application')
    launch_app_parser.add_argument('product_id', help='Product ID')
    launch_app_parser.add_argument('pod_id', help='Instance ID')
    launch_app_parser.add_argument('package_name', help='Package name')
    launch_app_parser.set_defaults(func=cmd_launch_app)

    close_app_parser = subparsers.add_parser('close-app', help='Close a running application')
    close_app_parser.add_argument('product_id', help='Product ID')
    close_app_parser.add_argument('pod_id', help='Instance ID')
    close_app_parser.add_argument('package_name', help='Package name')
    close_app_parser.set_defaults(func=cmd_close_app)

    uninstall_app_parser = subparsers.add_parser('uninstall-app', help='Uninstall an application')
    uninstall_app_parser.add_argument('product_id', help='Product ID')
    uninstall_app_parser.add_argument('pod_id', help='Instance ID')
    uninstall_app_parser.add_argument('app_id', help='Application ID')
    uninstall_app_parser.set_defaults(func=cmd_uninstall_app)

    auto_install_app_parser = subparsers.add_parser('auto-install-app', help='Automatically download and install an application')
    auto_install_app_parser.add_argument('product_id', help='Product ID')
    auto_install_app_parser.add_argument('--pod-id-list', required=True, help='Comma-separated list of instance IDs')
    auto_install_app_parser.add_argument('--install-type', type=int, choices=[0, 1], help='Install mode: 0=device-storage independent install, 1=application-image install')
    auto_install_app_parser.add_argument('--download-url', help='Application download URL')
    auto_install_app_parser.add_argument('--package-name', help='Package name')
    auto_install_app_parser.add_argument('--version-code', type=int, help='Application version code')
    auto_install_app_parser.add_argument('--image-id', help='Image package ID')
    auto_install_app_parser.add_argument('--absolute-path', help='Absolute path to application or image root directory')
    auto_install_app_parser.add_argument('--is-preinstall', action='store_true', default=None, help='Mark as pre-installed application')
    auto_install_app_parser.set_defaults(func=cmd_auto_install_app)

    get_app_list_parser = subparsers.add_parser('get-pod-app-list', help='Get the application list of an instance')
    get_app_list_parser.add_argument('product_id', help='Product ID')
    get_app_list_parser.add_argument('pod_id', help='Instance ID')
    get_app_list_parser.set_defaults(func=cmd_get_pod_app_list)

    detail_app_parser = subparsers.add_parser('detail-app', help='Get application details')
    detail_app_parser.add_argument('product_id', help='Product ID')
    detail_app_parser.add_argument('app_id', help='Application ID')
    detail_app_parser.set_defaults(func=cmd_detail_app)

    list_apps_parser = subparsers.add_parser('list-apps', help='List application information')
    list_apps_parser.add_argument('product_id', help='Product ID')
    list_apps_parser.add_argument('--app-id', help='Application ID')
    list_apps_parser.add_argument('--app-name', help='Application name')
    list_apps_parser.add_argument('--app-type', type=int, help='Application type')
    list_apps_parser.add_argument('--package-name-list', help='Comma-separated list of package names')
    list_apps_parser.add_argument('--max-results', type=int, default=10, help='Number of results per page')
    list_apps_parser.add_argument('--next-token', help='Pagination cursor')
    list_apps_parser.set_defaults(func=cmd_list_apps)

    list_app_version_deploys_parser = subparsers.add_parser('list-app-version-deploys', help='List application version deploy records')
    list_app_version_deploys_parser.add_argument('product_id', help='Product ID')
    list_app_version_deploys_parser.add_argument('app_id', help='Application ID')
    list_app_version_deploys_parser.add_argument('--version-id', help='Application version ID')
    list_app_version_deploys_parser.set_defaults(func=cmd_list_app_version_deploys)

    get_app_crash_log_parser = subparsers.add_parser('get-app-crash-log', help='Get application crash logs')
    get_app_crash_log_parser.add_argument('product_id', help='Product ID')
    get_app_crash_log_parser.add_argument('pod_id_list', help='Comma-separated list of instance IDs')
    get_app_crash_log_parser.add_argument('--start-time', type=int, required=True, help='Start time (Unix seconds)')
    get_app_crash_log_parser.add_argument('--end-time', type=int, required=True, help='End time (Unix seconds)')
    get_app_crash_log_parser.set_defaults(func=cmd_get_app_crash_log)

    install_apps_parser = subparsers.add_parser('install-apps', help='Batch install applications')
    install_apps_parser.add_argument('product_id', help='Product ID')
    install_apps_parser.add_argument('--pod-id', required=True, help='Instance ID')
    install_apps_parser.add_argument('--app-list', required=True, help='Application list; format AppId:VersionId,AppId2:VersionId2')
    install_apps_parser.add_argument('--install-type', type=int, help='Install mode')
    install_apps_parser.add_argument('--is-preinstall', action='store_true', help='Mark as pre-installed application')
    install_apps_parser.set_defaults(func=cmd_install_apps)

    upload_app_parser = subparsers.add_parser('upload-app', help='Upload an application')
    upload_app_parser.add_argument('product_id', help='Product ID')
    upload_app_parser.add_argument('--app-type', type=int, required=True, help='Application type')
    upload_app_parser.add_argument('--download-url', required=True, help='Application file download URL')
    upload_app_parser.add_argument('--app-id', help='Application ID')
    upload_app_parser.add_argument('--app-name', help='Application name')
    upload_app_parser.add_argument('--rotation', type=int, help='Orientation: 0=portrait, 270=landscape')
    upload_app_parser.add_argument('--app-desc', help='Application description')
    upload_app_parser.add_argument('--parse-flag', type=int, help='Parse mode')
    upload_app_parser.add_argument('--app-mode', choices=['Public', 'Private'], help='Application visibility')
    upload_app_parser.set_defaults(func=cmd_upload_app)

    update_app_parser = subparsers.add_parser('update-app', help='Update an application')
    update_app_parser.add_argument('product_id', help='Product ID')
    update_app_parser.add_argument('--app-id', required=True, help='Application ID')
    update_app_parser.add_argument('--app-name', help='Application name')
    update_app_parser.add_argument('--rotation', type=int, help='Orientation')
    update_app_parser.add_argument('--icon-url', help='Icon URL')
    update_app_parser.add_argument('--app-desc', help='Application description')
    update_app_parser.add_argument('--app-mode', choices=['Public'], help='Application visibility')
    update_app_parser.set_defaults(func=cmd_update_app)

    delete_app_parser = subparsers.add_parser('delete-app', help='Delete an application')
    delete_app_parser.add_argument('product_id', help='Product ID')
    delete_app_parser.add_argument('--app-id', required=True, help='Application ID')
    delete_app_parser.set_defaults(func=cmd_delete_app)

    delete_app_version_parser = subparsers.add_parser('delete-app-version', help='Delete an application version')
    delete_app_version_parser.add_argument('product_id', help='Product ID')
    delete_app_version_parser.add_argument('--version-id', required=True, help='Application version ID')
    delete_app_version_parser.set_defaults(func=cmd_delete_app_version)

    launch_apps_parser = subparsers.add_parser('launch-apps', help='Batch launch applications')
    launch_apps_parser.add_argument('product_id', help='Product ID')
    launch_apps_parser.add_argument('--pod-id', required=True, help='Instance ID')
    launch_apps_parser.add_argument('--package-name-list', required=True, help='Comma-separated list of package names')
    launch_apps_parser.set_defaults(func=cmd_launch_apps)
