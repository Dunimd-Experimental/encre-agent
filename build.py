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

# ---------------------------------------------------------------------------
# Module summary
# ---------------------------------------------------------------------------
# Top-level build orchestrator for the Encre project.
#
# This script builds the native Rust extension, compiles the desktop TypeScript
# front-end, marks the prebuilt ``node-pty`` binary so Electron's rebuild is
# skipped, packages the desktop installer, and finally copies the produced
# installer artifacts to the repository root.  It also deploys the bundled EA
# plugin packages into the unique plugin install directory
# (``~/.dunimd/encre/plugins``) so both the dev run (``npm start``) and the
# packaged app see the same plugin set — the runtime scanner looks ONLY there.
#
# Usage (run from the repository root):
#     python build.py          # full build (deps + rust + wheels + server + cli + desktop)
#     python build.py plugins  # deploy bundled plugin packages to the install dir
#     python build.py cli      # build only the standalone encre-plugin executable
#     python build.py vendor   # batch-build official packages -> dist/vendor/ for the registry
#     python build.py clean    # remove build artifacts
# ---------------------------------------------------------------------------

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
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
CLI_SRC = ROOT / "cli"  # encre-plugin source package
CLI_DIST = ROOT / "build" / "cli"  # PyInstaller output directory

IS_WIN = platform.system() == "Windows"
EXT = ".pyd" if IS_WIN else ".so"
SERVER_EXE = "encre-server.exe" if IS_WIN else "encre-server"
CLI_EXE = "encre-plugin.exe" if IS_WIN else "encre-plugin"

# Bundled EA plugin packages live at <repo>/core/ea_tools; the runtime plugin
# scanner looks exclusively at ~/.dunimd/encre/plugins, so both the dev run
# and the packaged app need them materialized there.
PLUGINS_SRC = ROOT / "core" / "ea_tools"
PLUGIN_INSTALL_DIR = Path.home() / ".dunimd" / "encre" / "plugins"

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

# harness/ and core/ declare requires-python = ">=3.14"; ea-cwh/cli need >=3.11.
MIN_PYTHON = (3, 14)

# Last-resort package index, used when the user's configured mirror is blocked.
DEFAULT_INDEX = "https://pypi.org/simple/"


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


def _probe_index(index_url: str, timeout: int = 10) -> bool:
    """True when ``index_url`` answers a real simple-index request."""
    req = urllib.request.Request(
        index_url.rstrip("/") + "/setuptools/",
        headers={"User-Agent": "pip/22.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ambient_index() -> str | None:
    """The index URL pip would use here (PIP_INDEX_URL, else pip.conf)."""
    url = os.environ.get("PIP_INDEX_URL")
    if url:
        return url
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "config", "get", "global.index-url"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    val = r.stdout.strip()
    # pip prints placeholders like <merged>/<env> when nothing is configured.
    return val if val and not val.startswith("<") else None


def reachable_index() -> str:
    """Return a package index that actually answers HTTP requests.

    Linux/WSL developer boxes routinely point pip at a university mirror that
    has since been blocked, retired or rate-limited.  When that happens pip
    reports "Could not find a version that satisfies the requirement
    setuptools>=68 (from versions: none)" -- an empty version list with no
    hint that the index itself refused the request.  Probe the configured
    index and fall back to the public PyPI index instead of that dead end.

    ``ENCRE_PIP_INDEX_URL`` forces a specific index (and skips the probe).
    """
    override = os.environ.get("ENCRE_PIP_INDEX_URL")
    if override:
        return override.rstrip("/") + "/"
    ambient = _ambient_index()
    candidates = [c.rstrip("/") + "/"
                  for c in dict.fromkeys([x for x in (ambient, DEFAULT_INDEX) if x])]
    for idx in candidates:
        if _probe_index(idx):
            if ambient and idx.rstrip("/") != ambient.rstrip("/"):
                print(f"  [pip] configured index is unreachable: {ambient}")
                print(f"  [pip] falling back to {idx}")
            return idx
    print(f"  [pip] no reachable index; using {DEFAULT_INDEX}")
    return DEFAULT_INDEX


def install_deps():
    """Install build-time dependencies for Python and Node.js.

    Pip-installs the ``harness`` and ``core`` source trees in editable mode so
    their declared dependencies land in the active interpreter (the same one
    PyInstaller later uses), plus the ``build``/``pyinstaller`` tooling.  The
    desktop Node dependencies are installed by ``npm ci`` further down.

    Three layers of hardening, all needed on Linux/WSL developer boxes:

      1. Resolve a reachable package index (see :func:`reachable_index`).
      2. Upgrade ``pip``, ``setuptools`` and ``wheel`` in the active
         interpreter first -- the Ubuntu distro ships setuptools 59, which
         cannot honour the ``setuptools>=68`` the pyproject build-system
         tables declare.
      3. Install the source trees with ``--no-build-isolation``.  All three
         use the plain ``setuptools.build_meta`` backend, so pip reuses the
         ambient setuptools/wheel from step 2 instead of spawning an isolated
         build-dependency subprocess that would have to re-download
         setuptools from the (possibly blocked) index -- the exact subprocess
         that dies with "No matching distribution found for setuptools>=68".
    """
    print("\n=== Installing Python build dependencies ===")
    index = reachable_index()
    idx = f'--index-url "{index}"'
    base = f'"{sys.executable}" -m pip install --disable-pip-version-check --no-input'
    print(f"  [pip] using index: {index}")

    run(f'{base} {idx} -U pip "setuptools>=68" wheel')
    # Editable installs of the three source trees (pure setuptools backend).
    run(f'{base} {idx} --no-build-isolation -e ./harness -e ./core -e ./ea-cwh')
    # Build tooling ships binary wheels; keep the isolated build env for them.
    run(f'{base} {idx} build pyinstaller')


def _python_version(exe: str) -> tuple[int, int] | None:
    """(major, minor) of an interpreter, or None when it cannot be probed."""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True,
                           text=True, timeout=20)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(\d+)\.(\d+)", out)
    except (OSError, subprocess.SubprocessError):
        return None
    return (int(m.group(1)), int(m.group(2))) if m else None


def _candidate_pythons() -> list[str]:
    """Interpreter candidates worth trying, best-first (current one excluded)."""
    out: list[str] = []
    override = os.environ.get("ENCRE_PYTHON")
    if override:
        out.append(override)
    for name in ("python3.14", "python", "python3"):
        found = shutil.which(name)
        if found:
            out.append(found)
    # Common locations that are not on PATH.
    out += [str(p) for p in sorted(Path("/usr/local/bin").glob("python3.1*"))]
    out += [str(p) for p in sorted(
        Path(os.path.expanduser("~/.pyenv/versions")).glob("3.1*/bin/python3"))]
    # uv-managed interpreters (the documented way to get 3.14): they are never
    # put on PATH, so probe the uv data dir directly.
    out += [str(p) for p in sorted(
        Path(os.path.expanduser("~/.local/share/uv/python")).glob(
            "cpython-3.1*/bin/python3"))]
    current = str(Path(sys.executable).resolve())
    return [p for p in dict.fromkeys(out)
            if str(Path(p).resolve()) != current]


def ensure_build_python() -> None:
    """Re-exec the build with a Python interpreter new enough for this repo.

    ``harness``/``core`` declare ``requires-python = ">=3.14"``.  On a Linux
    box whose only interpreter is the distribution Python (3.10/3.11/3.12) the
    editable installs fail inside the resolver, with no mention of the version
    floor -- and nothing later in the pipeline would work either, since the
    same interpreter is handed to PyInstaller.

    Preference order: ``ENCRE_PYTHON``, then any newer interpreter already on
    the box (PATH, /usr/local/bin, pyenv).  If none exists, print how to get
    one instead of letting pip fail cryptically.
    """
    cur = _python_version(sys.executable) or (0, 0)
    if cur >= MIN_PYTHON:
        return
    for cand in _candidate_pythons():
        ver = _python_version(cand)
        if ver and ver >= MIN_PYTHON:
            print(f"  [python] {sys.executable} is {cur[0]}.{cur[1]}, too old for "
                  f"this repo -- switching to {cand} ({ver[0]}.{ver[1]})")
            os.execv(cand, [cand, str(Path(__file__).resolve()), *sys.argv[1:]])
            return
    needed = ".".join(map(str, MIN_PYTHON))
    print(f"\n  [!!] build aborted: {sys.executable} is Python {cur[0]}.{cur[1]}, "
          f"but Encre Agent requires Python {needed}+")
    print("      harness/core declare requires-python >= 3.14, so pip cannot "
          "install them,")
    print("      and the same interpreter would later be bundled by PyInstaller.")
    print("      Install Python 3.14+ and point build.py at it:")
    print(f"          ENCRE_PYTHON=/path/to/python3.14 python3 build.py")
    print("      (or just run it directly, e.g. `python3.14 build.py`)")
    print("      To obtain one: `uv python install 3.14` (see `uv python find 3.14`)")
    print("      or `pyenv install 3.14`.  build.py also probes uv/pyenv/")
    print("      /usr/local/bin for a suitable interpreter on its own.")
    sys.exit(1)


def check_prerequisites(command: str) -> None:
    """Fail fast when a required toolchain binary is missing.

    The Rust, wheel and desktop steps each take minutes, so a missing binary
    should surface as one clear line up front rather than a mid-build failure
    of whichever step needs it.  Only the commands that actually need the tool
    are gated -- ``plugins``/``clean`` need nothing but Python.
    """
    print("\n=== Checking build prerequisites ===")
    names = ("python3", "cargo", "node", "npm", "make", "g++")
    found = {name: shutil.which(name) for name in names}
    for name in names:
        print(f"  [{found[name] or 'missing':<44}] {name}")

    required = set()
    if command in ("build", "cli", "vendor"):
        required.add("cargo")
    if command == "build":
        required |= {"node", "npm"}
        if not IS_WIN:
            # npm ci compiles node-pty from source via node-gyp.
            required |= {"make", "g++"}
    missing = [n for n in names if n in required and not found[n]]
    if missing:
        print(f"  [!!] missing required tool(s): {', '.join(missing)} -- aborting.")
        if not found["cargo"]:
            print("      Rust: https://rustup.rs")
        if not found["node"] or not found["npm"]:
            print("      Node: install a NATIVE build for this OS "
                  "(>= 20, npm 10+) from https://nodejs.org")
            if not IS_WIN and (shutil.which("npm") or shutil.which("npx")):
                print("      Note: an npm/npx shim from a Windows install is not "
                      "usable inside Linux/WSL.")
        if not IS_WIN:
            print("      Linux deps: build-essential plus the Electron runtime "
                  "libraries")
            print("      (libwebkit2gtk-4.0-37 libnss3 libatk1.0-0 "
                  "libatk-bridge2.0-0 libcups2 libdrm2 libgtk-3-0 libxss1 "
                  "libasound2).")
        sys.exit(1)


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


def deploy_plugins():
    """Deploy bundled EA plugin packages to the unique install directory.

    Copies every package from ``<repo>/core/ea_tools`` into
    ``~/.dunimd/encre/plugins`` (overwrite per package).  The runtime plugin
    scanner looks exclusively there, so this makes the dev run (``npm start``
    spawning the source backend) see the same plugin set as a packaged app.
    Third-party packages already present in the install dir are untouched;
    only names that exist in the bundled tree are managed here.
    """
    print("\n=== Deploying plugin packages to ~/.dunimd/encre/plugins ===")
    if not PLUGINS_SRC.is_dir():
        print(f"  [!!] No bundled plugin packages at {PLUGINS_SRC} -- skipped.")
        return
    PLUGIN_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for pkg in sorted(PLUGINS_SRC.iterdir()):
        if pkg.is_dir():
            shutil.copytree(
                pkg, PLUGIN_INSTALL_DIR / pkg.name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
            )
            count += 1
    print(f"  [OK] {count} plugin package(s) available at {PLUGIN_INSTALL_DIR}")


def build(locale_list: list[str] | None = None, publish_vendor: bool = False) -> None:
    """Run the full Encre build pipeline.

    Steps performed (in order):
      1. Install Python/Node build dependencies.
      2. Deploy bundled EA plugin packages to the unique install directory.
      3. Compile the Rust native extension and copy it into the Python package.
      4. Build importable wheels for the harness and core libraries.
      5. Bundle the Python backend into a standalone executable via PyInstaller.
      6. Compile the standalone encre-plugin CLI executable via PyInstaller.
      7. Compile the official tools/skills into wheels + catalog.d entries
         (dist/vendor/) — the same packages already bundled into the installer,
         re-built so they can be published to the central repository.
      8. Install desktop Node dependencies and build the TypeScript front-end
         (with the requested locale subset; default: all).
      9. Stamp a prebuilt ``node-pty`` marker so Electron source rebuild is skipped.
      10. Package the desktop installer via ``electron-builder``.
      11. Copy the resulting installer(s) to the repository root.
      12. If ``publish_vendor`` (``--publish-vendor``): upload the official wheels
         to PyPI and open the registry PR (the ONLY step that touches the network).
    """
    env = os.environ.copy()
    if sys.version_info >= (3, 14):
        env["PYO3_USE_ABI3_FORWARD_COMPATIBILITY"] = "1"

    target_arch = os.environ.get("TARGET_ARCH", "x64")

    # 1. Install Python build dependencies (harness/core deps + pyinstaller/build).
    install_deps()

    # 2. Deploy bundled plugin packages (dev + packaged apps share one dir).
    deploy_plugins()

    # 3. Compile the Rust native extension in release mode for the encre-py crate.
    run(f"cd native && cargo build --release -p encre-py", env=env)
    # Pick the real compiled binary: skip .d/.exp/.lib/.pdb auxiliary files.
    # Cargo prefixes Unix cdylib output with "lib" (lib_native.so/
    # lib_native.dylib) but leaves Windows bare (_native.dll), so accept both.
    exts = (".dll", ".pyd", ".so", ".dylib")
    src = next((f for f in sorted((NATIVE/"target"/"release").glob("*native*"))
                if f.suffix in exts and f.stem in ("_native", "lib_native")), None)
    if not src:
        # Hard failure: the Rust extension is a mandatory runtime component
        # (the permission engine and all native accel live in it), so the
        # build must never silently continue without it.
        print("  !!! native extension not found in native/target/release -- "
              "aborting: the package must never ship without the Encre Agent Core.")
        sys.exit(1)
    shutil.copy2(src, HARNESS_PKG / f"_native{EXT}")
    print(f"  [OK] native extension deployed: {src.name} -> harness/encre/_native{EXT}")

    # The `encre` package is a namespace split across harness/encre and
    # core/encre, and `_native` is the one module name both trees can carry.
    # Which copy wins depends on ordering: at runtime core/encre sorts first in
    # ``encre.__path__``, while the staged bundle copies harness first and core
    # second -- so a leftover binary in core/encre silently replaces the freshly
    # built one in production too. It belongs to the harness distribution
    # (only ``encre.native``, which lives in harness, imports it), so strip any
    # copy from the core tree rather than let a stale artifact shadow the build.
    for stale in CORE_PKG.glob("_native*"):
        if stale.suffix in exts:
            stale.unlink()
            print(f"  [OK] removed stale native copy from core tree: {stale}")

    # 3. Build importable wheels for the harness and core Python libraries.
    build_wheels()

    # 4. Bundle encre.server.app into a standalone executable with all dependencies.
    build_server()

    # 4b. Compile the standalone encre-plugin CLI executable for this OS.
    build_cli()

    # 4c. Compile the official tools/skills into wheels + catalog.d entries.
    # These are the SAME packages step 2/4 already bundled into the installer;
    # compiling them here produces the registry-ready artifacts so a build can
    # also (re-)publish them to the central repository as official plugins.
    build_vendor()

    # 8. Install desktop Node dependencies (locked) and build the TS front-end.
    locales = locale_list if locale_list is not None else list(ALL_REGISTRY_LOCALES)
    build_frontend(locales)

    # 9. Stamp the prebuilt node-pty binary so Electron source rebuild is skipped.
    meta = DESKTOP / "node_modules" / "node-pty" / "build" / "Release" / ".forge-meta"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(f"{target_arch}--146")

    # 10. Package the Encre Agent Desktop into an installer (no publishing).
    # Pass target arch so only the runner's native arch is built.
    cmd = "cd desktop && npx electron-builder --publish never"
    if target_arch in ("arm64", "x64"):
        cmd += f" --{target_arch}"
    run(cmd)

    # 11. Copy the produced installer(s) up to the repository root for easy access.
    for pattern in ("EA Setup*", "Encre*Setup*"):
        for f in (DESKTOP/"release").glob(pattern):
            shutil.copy2(f, ROOT / f.name)
            print(f"  >>> {f.name}")

    # 12. Optional: publish the compiled official plugins to the central repo.
    # The one network-touching step, and only when explicitly asked for.
    if publish_vendor:
        publish_vendor_registry()


def publish_vendor_registry() -> None:
    """Publish ``dist/vendor/`` (official wheels + catalog.d) to the registry.

    Uploads every compiled wheel to PyPI, then copies the generated
    ``catalog.d`` entries into the local registry checkout and lets the
    registry's own publish-index workflow regenerate + release ``ea-cwh``.
    This is deliberately the only step that reaches the network, gated behind
    ``--publish-vendor`` because a PyPI upload is irreversible.

    Requires ``TWINE_USERNAME``/``TWINE_PASSWORD`` and a ``gh``-authenticated
    registry checkout (``ENCRE_REGISTRY_DIR``, default ``../ea-cwh``).
    """
    print("\n=== Publishing official plugins to the central repository ===")
    staging = ROOT / "dist" / "vendor"
    wheels = sorted((staging / "wheels").glob("*"))
    entries = sorted((staging / "catalog.d").glob("*.json"))
    if not wheels or not entries:
        print("  [!!] nothing staged under dist/vendor -- run the build first")
        sys.exit(1)

    host = sys.executable
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(CLI_SRC), env.get("PYTHONPATH")]))

    print(f"  uploading {len(wheels)} artifact(s) to PyPI ...")
    r = subprocess.run([host, "-m", "twine", "upload", *[str(w) for w in wheels]],
                       cwd=str(ROOT), env=env)
    if r.returncode != 0:
        print("  [!!] twine upload failed")
        sys.exit(1)

    registry = Path(os.environ.get("ENCRE_REGISTRY_DIR", str(ROOT.parent / "ea-cwh")))
    if not (registry / ".git").is_dir():
        print(f"  [!!] no registry checkout at {registry} "
              "(set ENCRE_REGISTRY_DIR); wheels uploaded, PR not opened")
        sys.exit(1)
    (registry / "catalog.d").mkdir(exist_ok=True)
    for e in entries:
        shutil.copy2(e, registry / "catalog.d" / e.name)
    print(f"  staged {len(entries)} catalog.d entries into {registry}")
    print("  commit + push the registry to open the PR; publish-index.yml then "
          "regenerates ea-cwh and releases it to PyPI.")


def build_server():
    """Bundle the Python backend into a standalone executable via PyInstaller.

    Produces ``build/server/encre-server[.exe]`` containing the full Python
    runtime, all encre modules, prompt templates, skill definitions, and the
    Rust native extension.  The resulting binary can be spawned by the
    Electron Encre Agent Desktop without requiring a system Python installation.
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

    # Ship the entire EA packages tree as raw data.  At runtime (frozen) the
    # plugin scanner first DEPLOYS these packages from <bundle>/ea_tools into
    # the unique install dir (~/.dunimd/encre/plugins) and then scans only
    # that directory (encre.plugins.ea_scan): reads each package's
    # pyproject.toml, imports its plugin module from the deployed source, and
    # loads its bundled skills/.  No package is hardcoded here — dropping a
    # new package into core/ea_tools/ ships it automatically.
    # __pycache__ / egg-info are stripped from the copy.
    # (EA extension packages belong to the encre-core distribution; the seven
    # mandatory system tools live in harness as encre.tools.builtin.)
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
    # Ship the CURRENT central index snapshot inside the server bundle: the
    # market renders it offline from first launch (frozen builds refresh it
    # via HTTPS into the data-dir cache afterwards — encre.market_catalog).
    hidden_args.extend(["--hidden-import", "ea_cwh", "--collect-data", "ea_cwh"])

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
    for p in [ROOT / "harness", ROOT / "core", ROOT / "ea-cwh"]:
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

    # Hard-verify the Rust extension made it into the bundle: the staged
    # encre/ tree ships as raw data, so a missing file here would only
    # surface at runtime as an ImportError.  Fail the build instead.
    bundled_native = SERVER_DIST / "encre-server" / "_internal" / "encre" / f"_native{EXT}"
    if not bundled_native.exists():
        print(f"  [!!] ERROR: native extension missing from the server bundle: "
              f"{bundled_native} -- aborting so the app can never ship native-less.")
        sys.exit(1)
    print(f"  [OK] native extension present in server bundle: {bundled_native}")


def build_plugin_sdk():
    """Rebuild the distributable plugin UI SDK and embed it in the CLI."""
    print("\n=== Building plugin UI SDK ===")
    node = shutil.which("node")
    if not node:
        print("  [!!] ERROR: node is required to build the plugin UI SDK")
        sys.exit(1)
    run(f'"{node}" "{ROOT / "sdk" / "build.mjs"}"')
    src = ROOT / "sdk" / "dist"
    dst = CLI_SRC / "ea_plugin_cli" / "sdk_dist"
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)
    print(f"  [OK] SDK embedded in {dst}")


def build_cli():
    """Compile the encre-plugin CLI into a standalone executable for THIS OS.

    ``--onefile`` packs the interpreter plus the (stdlib-only) ea_plugin_cli
    package into a single download-and-run binary: ``encre-plugin.exe`` on
    Windows, extension-free on macOS/Linux — platform and architecture come
    from whatever machine runs the build (PyInstaller does not cross-compile).
    The exe still shells out to a host Python for build/verify/twine work, so
    nothing has to be pip-installed for the CLI itself.

    Output: ``build/cli/encre-plugin[.exe]``.
    """
    print("\n=== Building encre-plugin CLI (PyInstaller) ===")

    build_plugin_sdk()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        run(f'"{sys.executable}" -m pip install pyinstaller')

    if CLI_DIST.exists():
        shutil.rmtree(CLI_DIST)
    CLI_DIST.mkdir(parents=True, exist_ok=True)

    cmd_parts = [
        f'"{sys.executable}"', "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name", "encre-plugin",
        "--distpath", str(CLI_DIST),
        "--workpath", str(ROOT / "build" / "pyinstaller-work-cli"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(CLI_SRC),
        # ea_cwh rides along so `encre-plugin catalog regen|check` works in
        # the downloaded exe too (registry-side index validation).
        "--paths", str(ROOT / "ea-cwh"),
        "--hidden-import", "ea_cwh",
        # Plugin UI SDK ships as data so `encre-plugin init/sdk` works in the
        # downloaded onefile executable without a network fetch.
        "--add-data", f"{CLI_SRC / 'ea_plugin_cli' / 'sdk_dist'}{os.pathsep}ea_plugin_cli/sdk_dist",
        str(CLI_SRC / "pyi_entry.py"),
    ]
    run(" ".join(cmd_parts))

    output_exe = CLI_DIST / CLI_EXE
    if not output_exe.exists():
        print(f"  [!!] ERROR: Expected output not found at {output_exe}")
        sys.exit(1)
    size_mb = output_exe.stat().st_size / (1024 * 1024)
    print(f"  [OK] CLI bundled: {output_exe} ({size_mb:.1f} MB, "
          f"{platform.system()}/{platform.machine()})")


def build_vendor() -> None:
    """Batch-build the official EA packages into a registry-ready staging dir.

    Delegates entirely to ``encre-plugin vendor`` (the CLI owns the publish
    policy, tier gate, wheel build and sha256 digests — no duplication here).
    Produces ``dist/vendor/catalog.d/<name>.json`` + ``dist/vendor/wheels/``
    ready to publish to PyPI and PR into the ea-cwh registry.  This is a
    GENERATOR: it uploads nothing and opens no PR — reviewing/publishing is a
    separate, deliberate ``encre-plugin publish`` step.

    Optional flags pass through: ``--filter <substr>`` (repeatable) to build a
    subset, and ``--keep-going`` to continue past per-package failures.
    """
    print("\n=== Building official plugin catalogue (encre-plugin vendor) ===")
    cmd = [sys.executable, "-m", "ea_plugin_cli", "vendor",
           "--src", str(PLUGINS_SRC), "--out", str(ROOT / "dist" / "vendor")]
    for extra in _pass_through("filter"):
        cmd.append(extra)
    env = os.environ.copy()
    # The CLI lives in ./cli and is not necessarily pip-installed; put it on
    # the subprocess path so `-m ea_plugin_cli` resolves.
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(CLI_SRC), env.get("PYTHONPATH")]))
    r = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if r.returncode != 0:
        print("  [!!] vendor build failed")
        sys.exit(1)


def _pass_through(name: str) -> list[str]:
    """Re-surface ``--name`` occurrences from the build.py argv to the CLI."""
    out, argv, take = [], sys.argv[1:], False
    for tok in argv:
        if take and not tok.startswith("-"):
            out += [f"--{name}", tok]
            take = False
        elif tok == f"--{name}":
            take = True
        elif tok.startswith(f"--{name}="):
            out += [f"--{name}", tok.split("=", 1)[1]]
    return out


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
                        choices=["build", "plugins", "cli", "vendor", "clean"],
                        help="Command to run (default: build). 'plugins' only deploys "
                             "bundled plugin packages to ~/.dunimd/encre/plugins; "
                             "'cli' only builds the standalone encre-plugin executable; "
                             "'vendor' only batch-builds the official packages into "
                             "dist/vendor/ for registry publishing.")
    parser.add_argument("--locales", "-L",
                        default="all",
                        help='Locale filter for frontend build. Comma-separated alias list (e.g. "cn", "cn,ja", "all"). "en" is always included. Default: all.')
    parser.add_argument("--filter", action="append", default=[],
                        help="vendor only: package-name substring (repeatable)")
    parser.add_argument("--publish-vendor", action="store_true",
                        help="with the full build: after compiling, upload the official "
                             "wheels to PyPI and stage catalog.d into the registry checkout "
                             "(network + irreversible; off by default)")
    args = parser.parse_args()

    # Fail fast (or switch interpreter) before any step spends minutes on
    # dependencies: an interpreter below the repo's requires-python floor breaks
    # pip's resolver and would later be the very runtime PyInstaller bundles.
    ensure_build_python()
    check_prerequisites(args.command)

    if args.command == "clean":
        clean()
    elif args.command == "plugins":
        deploy_plugins()
    elif args.command == "cli":
        build_cli()
    elif args.command == "vendor":
        build_vendor()
    else:
        locale_list = _resolve_locales(args.locales)
        print(f"Locale build target: {locale_list}")
        build(locale_list, publish_vendor=args.publish_vendor)
