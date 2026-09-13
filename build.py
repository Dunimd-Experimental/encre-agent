#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

# ---------------------------------------------------------------------------
# Module summary
# ---------------------------------------------------------------------------
# Top-level build orchestrator for the Encre project.
#
# This script builds the native Rust extension, compiles the desktop TypeScript
# front-end, marks the prebuilt ``node-pty`` binary so Electron's rebuild is
# skipped, packages the desktop installer, and finally copies the produced
# installer artifacts to the repository root.
#
# Usage (run from the repository root):
#     python build.py          # full build (deps + rust + wheels + server + desktop)
#     python build.py clean    # remove build artifacts
# ---------------------------------------------------------------------------

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NATIVE = ROOT / "native"
DESKTOP = ROOT / "desktop"
# The Python package "encre" spans two source trees: the pure library lives in
# harness/encre and the server layer (server/gateway/channels/iclaw) in
# core/encre.  PyInstaller needs both on its module search path.
HARNESS_PKG = ROOT / "harness" / "encre"
CORE_PKG = ROOT / "core" / "encre"
SERVER_DIST = ROOT / "build" / "server"  # PyInstaller output directory

IS_WIN = platform.system() == "Windows"
EXT = ".pyd" if IS_WIN else ".dylib" if platform.system() == "Darwin" else ".so"
SERVER_EXE = "encre-server.exe" if IS_WIN else "encre-server"

# Locale alias map: user-friendly short names →canonical registry keys.
# "en" is always injected as a mandatory fallback regardless of input.
LOCALE_ALIASES: dict[str, str] = {
    "cn": "zh",
    "zh": "zh",
    "zh-hant": "zh-Hant",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "de": "de",
    "tr": "tr",
    "es": "es",
    "pt": "pt",
    "ar": "ar",
    "he": "he",
}

ALL_REGISTRY_LOCALES = ["zh", "en", "zh-Hant", "ja", "ko", "de", "es", "pt", "tr", "ar", "he"]

def run(cmd, **kw):
    """Run a shell command, echoing it first.

    Args:
        cmd: The shell command string to execute.
        **kw: Extra keyword arguments forwarded to :func:`subprocess.run`.
              ``cwd`` may be given to override the default repository root
              (used e.g. for ``python -m build`` which would otherwise
              resolve to this repo's own ``build.py``).
    """
    print(f"$ {cmd}")
    cwd = kw.pop("cwd", ROOT)
    subprocess.run(cmd, shell=True, check=True, cwd=cwd, **kw)

def install_deps():
    """Install build-time dependencies for Python and Node.js.

    Pip-installs the ``harness`` and ``core`` source trees in editable mode so
    their declared dependencies land in the active interpreter (the same one
    PyInstaller later uses), plus the ``build``/``pyinstaller`` tooling.  The
    desktop Node dependencies are installed by ``npm ci`` further down.
    """
    print("\n=== Installing Python build dependencies ===")
    run(f'"{sys.executable}" -m pip install -e ./harness -e ./core pyinstaller build')


def build_wheels():
    """Build importable library wheels for the Python packages.

    Produces ``dist/wheels/encre_harness-*.whl`` and ``dist/wheels/encre_core-*.whl``
    so the harness/core libraries can be pip-installed independently of the
    PyInstaller server executable.
    """
    print("\n=== Building Python library wheels ===")
    wheels = ROOT / "dist" / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    # Run from a neutral working directory: `python -m build` must resolve to
    # the PyPA `build` package, not this repo's own build.py.
    neutral = ROOT / "build"
    neutral.mkdir(parents=True, exist_ok=True)
    for proj in ("harness", "core"):
        run(f'"{sys.executable}" -m build --wheel --outdir "{wheels}" "{ROOT / proj}"', cwd=neutral)
    for whl in sorted(wheels.glob("*.whl")):
        print(f"  >>> {whl.name}")


def _resolve_locales(cli_input: str) -> list[str]:
    """Resolve a --locales CLI value into a sorted list of canonical locale keys.

    Rules:
      - Aliases in LOCALE_ALIASES are expanded (e.g. "cn" →"zh").
      - "en" is always included as a mandatory fallback.
      - "all" selects every locale in ALL_REGISTRY_LOCALES.
      - Input is comma-separated (e.g. "cn,ja" or "all").
    """
    if cli_input == "all":
        return list(ALL_REGISTRY_LOCALES)
    selected: set[str] = set()
    for token in cli_input.replace(" ", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        canonical = LOCALE_ALIASES.get(token)
        if canonical is None:
            print(f"  [locale] unknown alias '{token}', skipping.")
            continue
        selected.add(canonical)
    # English is mandatory
    selected.add("en")
    # Preserve original display order, filter to only selected locales
    result = [loc for loc in ALL_REGISTRY_LOCALES if loc in selected]
    return result


def build_frontend(locale_list: list[str]) -> None:
    """Install desktop Node deps and build the TypeScript renderer with the given locales.

    Passes the locale list via the LOCALES environment variable so that esbuild's
    --define:BUILD_LOCALES_LIST conditional compiles only the requested locales
    into the bundle (unused locale modules are tree-shaken away).
    """
    print("\n=== Building frontend (locales: {}) ===".format(locale_list))
    run("cd desktop && npm ci")
    env = os.environ.copy()
    env["LOCALES"] = ",".join(locale_list)
    run("cd desktop && node build.js", env=env)


def build(locale_list: list[str] | None = None) -> None:
    """Run the full Encre build pipeline.

    Steps performed (in order):
      1. Install Python/Node build dependencies.
      2. Compile the Rust native extension and copy it into the Python package.
      3. Build importable wheels for the harness and core libraries.
      4. Bundle the Python backend into a standalone executable via PyInstaller.
      5. Install desktop Node dependencies and build the TypeScript front-end
         (with the requested locale subset; default: all).
      6. Stamp a prebuilt ``node-pty`` marker so Electron source rebuild is skipped.
      7. Package the desktop installer via ``electron-builder``.
      8. Copy the resulting installer(s) to the repository root.
    """
    env = os.environ.copy()
    if sys.version_info >= (3, 14):
        env["PYO3_USE_ABI3_FORWARD_COMPATIBILITY"] = "1"

    target_arch = os.environ.get("TARGET_ARCH", "x64")

    # 1. Install Python build dependencies (harness/core deps + pyinstaller/build).
    install_deps()

    # 2. Compile the Rust native extension in release mode for the encre-py crate.
    run(f"cd native && cargo build --release -p encre-py", env=env)
    # Pick the real compiled binary: skip .d/.exp/.lib/.pdb auxiliary files.
    exts = (".dll", ".pyd", ".so", ".dylib")
    src = next((f for f in sorted((NATIVE/"target"/"release").glob("_native*"))
                if f.suffix in exts), None)
    if src: shutil.copy2(src, HARNESS_PKG / f"_native{EXT}")

    # 3. Build importable wheels for the harness and core Python libraries.
    build_wheels()

    # 4. Bundle encre.server.app into a standalone executable with all dependencies.
    build_server()

    # 5. Install desktop Node dependencies (locked) and build the TS front-end.
    locales = locale_list if locale_list is not None else list(ALL_REGISTRY_LOCALES)
    build_frontend(locales)

    # 6. Stamp the prebuilt node-pty binary so Electron source rebuild is skipped.
    meta = DESKTOP / "node_modules" / "node-pty" / "build" / "Release" / ".forge-meta"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(f"{target_arch}--146")

    # 7. Package the desktop app into an installer (no publishing).
    # Pass target arch so only the runner's native arch is built.
    cmd = "cd desktop && npx electron-builder --publish never"
    if target_arch in ("arm64", "x64"):
        cmd += f" --{target_arch}"
    run(cmd)

    # 8. Copy the produced installer(s) up to the repository root for easy access.
    for pattern in ("EA Setup*", "Encre*Setup*"):
        for f in (DESKTOP/"release").glob(pattern):
            shutil.copy2(f, ROOT / f.name)
            print(f"  >>> {f.name}")

def build_server():
    """Bundle the Python backend into a standalone executable via PyInstaller.

    Produces ``build/server/encre-server[.exe]`` containing the full Python
    runtime, all encre modules, prompt templates, skill definitions, and the
    Rust native extension.  The resulting binary can be spawned by the
    Electron desktop app without requiring a system Python installation.
    """
    print("\n=== Building Python backend (PyInstaller) ===")

    # Ensure PyInstaller is available
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        run(f'"{sys.executable}" -m pip install pyinstaller')

    # Clean previous build
    if SERVER_DIST.exists():
        shutil.rmtree(SERVER_DIST)
    SERVER_DIST.mkdir(parents=True, exist_ok=True)

    # Ship the complete encre source tree (harness/ + core/) as raw data.
    # The encre package is a pkgutil namespace split across two trees:
    # harness/encre (library) and core/encre (server/gateway/channels/spec/...).
    # PyInstaller's static modulegraph cannot follow pkgutil.extend_path across
    # the trees, and enumerating every module as a --hidden-import blows past
    # the Windows command-line length limit.  Instead we stage one merged tree
    # on disk and let the runtime extend_path in encre/__init__.py resolve
    # every module (and ship the data files: prompts/, skills/, dangerous
    # commands, _native.pyd) from <bundle>/encre/ at startup.  __pycache__ and
    # egg-info are stripped from the staged copy.
    encre_stage = ROOT / "build" / "encre_stage"
    if encre_stage.exists():
        shutil.rmtree(encre_stage)
    for src in (ROOT / "harness" / "encre", ROOT / "core" / "encre"):
        if src.is_dir():
            shutil.copytree(src, encre_stage, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    datas = [(str(encre_stage), "encre")]

    # Ship the entire EA packages tree.  At runtime the plugin registry
    # auto-scans <bundle>/ea_tools/ (encre.plugins.ea_scan): reads each
    # ea-tool-* package's pyproject.toml, imports its plugin module from
    # the shipped source, and loads its bundled skills/.  No package is
    # hardcoded here — dropping a new package into core/ea_tools/ ships it
    # automatically.  __pycache__ / egg-info are stripped from the copy.
    # (EA extension packages belong to the encre-core distribution; the six
    # mandatory primitives live in harness as encre.tools.builtin.)
    ea_src = ROOT / "core" / "ea_tools"
    if ea_src.is_dir():
        ea_stage = ROOT / "build" / "ea_tools_stage"
        if ea_stage.exists():
            shutil.rmtree(ea_stage)
        shutil.copytree(ea_src, ea_stage,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        datas.append((str(ea_stage), "ea_tools"))

    # Build --add-data arguments
    sep = ";" if IS_WIN else ":"
    data_args = []
    for src, dst in datas:
        data_args.extend(["--add-data", f"{src}{sep}{dst}"])

    # No encre.* hidden imports are needed: the entire merged source tree is
    # shipped as raw data and loaded from disk at runtime (see datas above),
    # so PyInstaller only needs the third-party modules that belong in the
    # PYZ.  Everything encre.* resolves from <bundle>/encre/ via extend_path.
    hidden_imports = [
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
        "numpy",
        "pydantic",
        "httpx",
        "websockets",
        "cryptography",
    ]
    hidden_args = []
    for mod in hidden_imports:
        hidden_args.extend(["--hidden-import", mod])

    # Exclude heavy optional dependencies to keep bundle smaller
    excludes = [
        "torch", "transformers", "tensorflow", "keras",
        "boto3", "botocore",
        "matplotlib", "scipy", "pandas",
        "tkinter", "unittest",
    ]
    exclude_args = []
    for mod in excludes:
        exclude_args.extend(["--exclude-module", mod])

    # Module search path: PyInstaller must see BOTH source trees so the
    # pkgutil-extended "encre" package resolves harness and core subpackages.
    # EA packages need no --paths/--hidden-import: they ship as raw source
    # data and the runtime scanner inserts their dirs into sys.path itself.
    path_args = []
    for p in [ROOT / "harness", ROOT / "core"]:
        path_args.extend(["--paths", str(p)])

    # Run PyInstaller
    cmd_parts = [
        f'"{sys.executable}"', "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", "encre-server",
        "--distpath", str(SERVER_DIST),
        "--workpath", str(ROOT / "build" / "pyinstaller-work"),
        "--specpath", str(ROOT / "build"),
        *data_args,
        *hidden_args,
        *exclude_args,
        *path_args,
        "--contents-directory", "_internal",
        str(CORE_PKG / "server" / "app.py"),
    ]
    run(" ".join(cmd_parts))

    # Verify output
    output_exe = SERVER_DIST / "encre-server" / SERVER_EXE
    if output_exe.exists():
        size_mb = output_exe.stat().st_size / (1024 * 1024)
        print(f"  [OK] Backend bundled: {output_exe} ({size_mb:.1f} MB)")
    else:
        print(f"  [!!] ERROR: Expected output not found at {output_exe}")
        sys.exit(1)


def clean():
    """Remove all build artifacts produced by :func:`build`.

    Deletes the Rust ``target`` directory, the compiled native extension inside
    the Python package, desktop build outputs (``dist``, renderer assets,
    ``release``), the PyInstaller server bundle, and any installer artifacts
    at the repository root.
    """
    for d in [NATIVE/"target", ROOT/"build", ROOT/"dist"]:
        if d.exists(): shutil.rmtree(d)
    for f in HARNESS_PKG.glob("_native.*"): f.unlink()
    for p in [DESKTOP/"dist", DESKTOP/"renderer"/"vs",
              DESKTOP/"renderer"/"bundle.js", DESKTOP/"renderer"/"xterm.css",
              DESKTOP/"release"]:
        if p.exists(): shutil.rmtree(p) if p.is_dir() else p.unlink()
    for pattern in ("Encre*Setup*", "EA Setup*"):
        for f in ROOT.glob(pattern): f.unlink()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encre build orchestrator")
    parser.add_argument("command", nargs="?", default="build",
                        choices=["build", "clean"],
                        help="Command to run (default: build)")
    parser.add_argument("--locales", "-L",
                        default="all",
                        help='Locale filter for frontend build. Comma-separated alias list (e.g. "cn", "cn,ja", "all"). "en" is always included. Default: all.')
    args = parser.parse_args()

    if args.command == "clean":
        clean()
    else:
        locale_list = _resolve_locales(args.locales)
        print(f"Locale build target: {locale_list}")
        build(locale_list)
