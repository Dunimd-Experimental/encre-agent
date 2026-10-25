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

"""Translation tool implementation for Encre Agent.

This module exposes a single ``translation`` tool that multiplexes three
operations — ``translate``, ``detect``, and ``languages`` — over a set of
pluggable backends. The design goal is "just work" for casual usage while
still letting privacy-conscious users route traffic through a private
engine:

* **Google** via ``deep-translator`` (keyless, best quality). The import
  is deferred to call time so this module can be loaded in environments
  where the extra package is not installed.
* **MyMemory** via a free HTTP endpoint (keyless, but requires an
  explicit source language and caps each request at ~500 characters).
* **LibreTranslate** via a self-hosted HTTP server (fully private, but
  requires the user to run the service and pass ``engine_url``).
* **Heuristic fallback** — a pure-Python Unicode-script scan that gives a
  plausible language code even when every network endpoint is down.

All network calls run through ``urllib`` inside a synchronous helper
which is dispatched to a thread via ``asyncio.to_thread``-style
``run_in_executor`` so the harness event loop stays responsive while we
wait for slow upstream APIs. Failures inside a provider are captured and
the next provider in the fallback chain is tried; if every provider
fails the tool returns a human-readable summary of the errors instead of
raising, because the harness treats a returned string as a normal
tool result and a raised exception as a system-level failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool

# Endpoint constants. Kept at module scope so they can be monkeypatched
# in tests without touching the call sites.
_MYMEMORY_URL = "https://api.mymemory.translated.net/get"
_GOOGLE_DETECT_URL = "https://translate.googleapis.com/translate_a/single"
_MYMEMORY_MAX_CHARS = 450
# MyMemory expects RFC3066-style codes for some languages.
_MYMEMORY_CODE_MAP = {"zh": "zh-CN", "tw": "zh-TW", "he": "iw-IL"}

# Unicode script ranges used as the final offline detection fallback.
# The ``break`` in the loop below means the ranges are evaluated top-to-bottom
# and the first match wins, so order matters where ranges overlap (they
# mostly don't, but CJK unifies ideographs shared by Chinese and Japanese;
# Japanese is preferred because kana is checked earlier for higher confidence).
_SCRIPT_RANGES: list[tuple[int, int, str]] = [
    (0x3040, 0x30FF, "ja"),   # Hiragana + Katakana
    (0xAC00, 0xD7AF, "ko"),   # Hangul
    (0x4E00, 0x9FFF, "zh-CN"),  # CJK unified ideographs
    (0x0400, 0x04FF, "ru"),   # Cyrillic
    (0x0600, 0x06FF, "ar"),   # Arabic
    (0x0590, 0x05FF, "he"),   # Hebrew
    (0x0E00, 0x0E7F, "th"),   # Thai
    (0x0370, 0x03FF, "el"),   # Greek
    (0x0900, 0x097F, "hi"),   # Devanagari
]


def _mymemory_code(lang: str) -> str:
    """Map a short language code to the form MyMemory accepts.

    Args:
        lang: A caller-supplied language code such as ``"zh"``.

    Returns:
        The RFC3066 form expected by MyMemory (``"zh-CN"``), or the
        original value if no translation is needed.
    """
    return _MYMEMORY_CODE_MAP.get(lang, lang)


def _chunk_text(text: str, limit: int = _MYMEMORY_MAX_CHARS) -> list[str]:
    """Split *text* into chunks no longer than ``limit`` (sentence-aware).

    The chunker is conservative: it first breaks on newlines (preserving
    paragraph structure) and only then on spaces. This is required because
    MyMemory enforces a hard per-request character limit — overlong bodies
    return an error rather than silently truncating, so we must fit each
    chunk below the ceiling ourselves.

    Args:
        text: Raw input text. ``\\r\\n`` is normalised to ``\\n`` so
            line-based splits behave the same on Windows-authored files.
        limit: Maximum characters per chunk. Defaults to the MyMemory
            ceiling but is parameterised so callers can pass a tighter
            bound for other providers.

    Returns:
        A list of chunks whose concatenation (with ``\\n`` join) is
        equivalent to the input up to whitespace at chunk boundaries.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for part in text.replace("\r\n", "\n").split("\n"):
        while len(part) > limit:
            cut = part.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(part[:cut])
            part = part[cut:].lstrip()
        if len(current) + len(part) + 1 > limit:
            if current:
                chunks.append(current)
            current = part
        else:
            current = f"{current}\n{part}" if current else part
    if current:
        chunks.append(current)
    return chunks


def _script_detect(text: str) -> tuple[str, str]:
    """Offline Unicode-script fallback detector; returns (code, confidence).

    This is the last-resort path: it is used only when every networked
    provider has failed or is unavailable, and it is deliberately
    coarse-grained. Japanese wins on any kana hit (kana are unambiguous
    — Cyrillic, Greek and Arabic scripts have no kana range), otherwise
    we pick the script with the most characters and report ``high``
    confidence. When no script matches we fall back to ``en`` with
    ``low`` confidence, on the assumption that untranslated ASCII text
    is far more likely to be English than some other unscripted
    language.
    """
    counts: dict[str, int] = {}
    kana = 0
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:
            kana += 1
        for lo, hi, lang in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if kana > 0:
        return "ja", "high"
    if counts:
        best = max(counts, key=lambda k: counts[k])
        return best, "high"
    return "en", "low"


def _google_detect(text: str) -> str:
    """Detect the language of *text* via Google's public translate endpoint.

    Uses the ``translate_a/single`` URL with ``sl=auto`` which returns the
    inferred source language at index ``2`` of the JSON array. This is a
    widely-used unofficial endpoint — no API key is required and there is
    no documented rate limit, but the URL scheme is not guaranteed to be
    stable across Google's releases.

    Only the first 200 characters are sent to keep the request cheap and
    avoid accidental data leakage of the full text.

    Raises:
        RuntimeError: If the endpoint returns an empty detection result.
    """
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text[:200],
    })
    req = urllib.request.Request(
        f"{_GOOGLE_DETECT_URL}?{params}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    detected = data[2]
    if not detected:
        raise RuntimeError("empty detection result")
    return str(detected)


def _google_translate(text: str, src: str, tgt: str) -> str:
    """Translate *text* via deep-translator's GoogleTranslator (keyless).

    The import of ``deep_translator`` is deferred to call time so that
    importing this module does not force the extra dependency into
    memory for callers that only need ``detect`` or ``languages``.
    Missing package raises ``ImportError`` which the executor catches
    and reports as ``missing dependency`` in the error summary.

    Args:
        text: The source text to translate.
        src: Source language code, or empty/``"auto"`` for auto-detect.
        tgt: Target language code.

    Returns:
        The translated string.

    Raises:
        ImportError: If the ``deep_translator`` package is missing.
        RuntimeError: If the translator returns an empty string.
    """
    from deep_translator import GoogleTranslator

    result = GoogleTranslator(source=src or "auto", target=tgt).translate(text)
    if not result:
        raise RuntimeError("GoogleTranslator returned no text")
    return str(result)


def _mymemory_translate(text: str, src: str, tgt: str) -> str:
    """Translate *text* via the free MyMemory API (keyless, explicit source).

    MyMemory requires an explicit source code (no auto-detection), so the
    caller must resolve one through :func:`_resolve_source` before
    entering here. Because each request is capped at ``_MYMEMORY_MAX_CHARS``,
    we chunk the input first and reassemble with newlines so paragraph
    structure survives.

    Raises:
        RuntimeError: On non-200 ``responseStatus`` or empty translation.
    """
    out: list[str] = []
    for chunk in _chunk_text(text):
        params = urllib.parse.urlencode({
            "q": chunk,
            "langpair": f"{_mymemory_code(src)}|{_mymemory_code(tgt)}",
        })
        req = urllib.request.Request(
            f"{_MYMEMORY_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("responseStatus") not in (200, "200"):
            raise RuntimeError(f"MyMemory error: {data.get('responseDetails')}")
        translated = data.get("responseData", {}).get("translatedText", "")
        if not translated:
            raise RuntimeError("MyMemory returned no text")
        out.append(str(translated))
    return "\n".join(out)


def _libre_detect(text: str, engine_url: str) -> str:
    """Detect via a LibreTranslate-compatible server.

    Handles both response shapes LibreTranslate has shipped historically:
    a bare object for a single-locale request and a list of objects for
    multi-locale servers. When neither is populated we return the empty
    string so the caller can fall through to the next provider.

    Args:
        text: The text to inspect.
        engine_url: Base URL of the LibreTranslate service, without a
            trailing slash. Defaults to ``http://localhost:5000``.
    """
    api_url = (engine_url or "http://localhost:5000").rstrip("/")
    payload = json.dumps({"q": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/detect", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    items = result if isinstance(result, list) else [result]
    return str(items[0].get("language", "")) if items else ""


def _libre_translate(text: str, src: str, tgt: str, engine_url: str, preserve_format: bool) -> str:
    """Translate *text* via a LibreTranslate-compatible server.

    Args:
        text: The text to translate.
        src: Source language code.
        tgt: Target language code.
        engine_url: Base URL of the LibreTranslate service.
        preserve_format: When ``True`` the payload requests HTML
            translation so embedded markup survives; otherwise plain
            text mode is used.

    Raises:
        RuntimeError: If the response contains no translated text.
    """
    api_url = (engine_url or "http://localhost:5000").rstrip("/")
    payload = json.dumps({
        "q": text, "source": src, "target": tgt,
        "format": "html" if preserve_format else "text",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/translate", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    translated = result.get("translatedText", "")
    if not translated:
        raise RuntimeError("LibreTranslate returned no text")
    return str(translated)


def _resolve_source(text: str, source_lang: str, engine_url: str) -> str:
    """Resolve an explicit source code for providers that lack auto-detect.

    MyMemory is the main consumer of this helper: it refuses requests
    with ``sl=auto``. When the caller already supplied a non-auto source
    we short-circuit; otherwise we probe Google and LibreTranslate in
    turn, and only fall back to the offline :func:`_script_detect`
    heuristic when both network probes fail. Capturing errors (rather
    than propagating) is important here — a downstream translation
    attempt is more valuable than a hard failure when detection is
    degraded.

    Args:
        text: The text whose source language is unknown.
        source_lang: The caller-provided source code, or ``"auto"``.
        engine_url: LibreTranslate base URL used by the libre probe.

    Returns:
        The best-effort source language code. Never raises.
    """
    if source_lang and source_lang != "auto":
        return source_lang
    errors: list[str] = []
    for probe, fn in (("google", lambda: _google_detect(text)),
                      ("libre", lambda: _libre_detect(text, engine_url))):
        try:
            code = fn()
            if code:
                return code
        except Exception as e:
            errors.append(f"{probe}: {e}")
    code, _confidence = _script_detect(text)
    return code


async def _translation_execute(**kwargs: Any) -> str:
    """Executor for the ``translation`` tool.

    The async signature is required by :func:`build_tool` so the tool
    scheduler can treat translation uniformly with other async tools.
    All the actual network work is synchronous; we marshal it into a
    thread pool via ``loop.run_in_executor`` so that a slow upstream
    does not stall the rest of the harness event loop.

    Args:
        kwargs: A mapping of the tool's declared inputs. The relevant
            keys are ``action``, ``text``, ``source_lang``,
            ``target_lang``, ``file_path``, ``service``, ``engine_url``,
            and ``preserve_format``. Missing keys fall back to sensible
            defaults (``auto`` for source/service, ``en`` for target).

    Returns:
        A JSON string describing the outcome. Errors are returned as a
        string, never raised, so the harness can surface them as a
        normal tool result.

    Raises:
        Never — all exceptions are captured and encoded into the
        returned string.
    """
    action = kwargs.get("action", "")
    text = kwargs.get("text", "")
    source_lang = kwargs.get("source_lang", "auto")
    target_lang = kwargs.get("target_lang", "en")
    file_path = kwargs.get("file_path", "")
    service = kwargs.get("service", "auto")
    engine_url = kwargs.get("engine_url", "")
    preserve_format = kwargs.get("preserve_format", False)

    loop = asyncio.get_event_loop()

    if action in ("translate", "detect"):
        if not text and not file_path:
            return "Missing required field: text or file_path"
        if file_path:
            if not os.path.exists(file_path):
                return f"File not found: {file_path}"
            text = Path(file_path).read_text(encoding="utf-8")

        def _translate() -> str:
            """Run the synchronous translation pipeline for one request.

            Provider selection is data-driven: the ``service`` kwarg
            chooses a fallback chain, and each provider is tried in
            order until one succeeds. Errors from earlier providers are
            accumulated into a single summary line so the caller sees
            the whole picture even when multiple backends fail for
            different reasons.
            """
            if service not in ("auto", "google", "mymemory", "libre"):
                return f"Unsupported service: {service}. Supported: auto, google, mymemory, libre"
            errors: list[str] = []

            if action == "detect":
                providers = {
                    "auto": ("google", "libre", "heuristic"),
                    "google": ("google",),
                    "libre": ("libre",),
                    "mymemory": ("heuristic",),
                }[service]
                for provider in providers:
                    try:
                        if provider == "google":
                            return json.dumps(
                                {"language": _google_detect(text), "method": "google"},
                                ensure_ascii=False, indent=2,
                            )
                        if provider == "libre":
                            return json.dumps(
                                {"language": _libre_detect(text, engine_url), "method": "libre"},
                                ensure_ascii=False, indent=2,
                            )
                        code, confidence = _script_detect(text)
                        return json.dumps(
                            {"language": code, "method": "script-heuristic", "confidence": confidence},
                            ensure_ascii=False, indent=2,
                        )
                    except Exception as e:
                        errors.append(f"{provider}: {e}")
                return f"Language detection failed ({'; '.join(errors)})"

            # action == "translate": try the selected provider chain until one succeeds.
            providers = {
                "auto": ("google", "mymemory", "libre"),
                "google": ("google",),
                "mymemory": ("mymemory",),
                "libre": ("libre",),
            }[service]
            for provider in providers:
                try:
                    if provider == "google":
                        translated = _google_translate(text, source_lang, target_lang)
                    elif provider == "mymemory":
                        src = _resolve_source(text, source_lang, engine_url)
                        translated = _mymemory_translate(text, src, target_lang)
                    else:
                        translated = _libre_translate(text, source_lang, target_lang, engine_url, preserve_format)
                    output = {
                        "source_lang": source_lang,
                        "target_lang": target_lang,
                        "service": provider,
                        "input_text": text[:500] + ("..." if len(text) > 500 else ""),
                        "translated_text": translated,
                    }
                    return json.dumps(output, ensure_ascii=False, indent=2)
                except ImportError as e:
                    errors.append(f"{provider}: missing dependency {e}")
                except Exception as e:
                    errors.append(f"{provider}: {e}")
            return f"Translation failed ({'; '.join(errors)})"

        result = await loop.run_in_executor(None, _translate)

        # When translating from a file, we additionally write the result next to
        # the source with a ``_<target>`` suffix so batch workflows can
        # pipeline multiple source files without manual copy/paste. Failures
        # here are swallowed: the translation itself is still useful in the
        # returned payload, and a disk error should not erase that result.
        if file_path:
            try:
                translated = json.loads(result).get("translated_text", "")
                if translated:
                    out_path = str(Path(file_path).with_stem(Path(file_path).stem + f"_{target_lang}"))
                    Path(out_path).write_text(translated, encoding="utf-8")
                    return f"Translation saved to {out_path}\n\n{result}"
            except (json.JSONDecodeError, OSError):
                pass

        return result

    elif action == "languages":
        def _languages() -> str:
            """List supported languages from the selected provider(s).

            MyMemory has no machine-readable language list — it just
            accepts ISO 639-1 codes plus RFC3066 variants — so we return
            a short explanatory note in that case rather than a bare
            error, because the caller can still drive it by code.
            """
            errors: list[str] = []
            providers = {
                "auto": ("google", "libre"),
                "google": ("google",),
                "libre": ("libre",),
                "mymemory": (),
            }.get(service)
            if providers is None:
                return f"Unsupported service: {service}"
            if service == "mymemory":
                return json.dumps({
                    "note": "MyMemory accepts ISO 639-1 codes plus RFC3066 variants "
                            "(e.g. zh-CN, en-GB); use service='google' or 'libre' "
                            "for a full machine-readable list.",
                }, ensure_ascii=False, indent=2)
            for provider in providers:
                try:
                    if provider == "google":
                        from deep_translator import GoogleTranslator
                        langs = GoogleTranslator().get_supported_languages(as_dict=True)
                        return json.dumps(langs, ensure_ascii=False, indent=2)
                    api_url = (engine_url or "http://localhost:5000").rstrip("/")
                    req = urllib.request.Request(f"{api_url}/languages")
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    return json.dumps(result, ensure_ascii=False, indent=2)
                except Exception as e:
                    errors.append(f"{provider}: {e}")
            return f"Failed to list languages ({'; '.join(errors)})"

        return await loop.run_in_executor(None, _languages)

    return f"Unknown action: {action}. Supported: translate, detect, languages"


translation_tool = build_tool(
    name="translation",
    description=(
        "Translate text between languages, detect the source language, or list "
        "supported languages. Backends: Google (deep-translator), MyMemory "
        "(free keyless HTTP API), LibreTranslate (self-hosted/private). "
        "Use `translate` to convert text/files, `detect` to identify a language, or "
        "`languages` to enumerate supported codes. "
        "Do NOT use this for speech-to-English (use translate_audio), for "
        "summarization, or for in-app i18n string catalogs. "
        "Tips: the default `service='auto'` falls back through google -> mymemory -> "
        "libre, so it works even when Google is unreachable; `detect` additionally "
        "falls back to an offline Unicode-script heuristic. When translating a file, "
        "the result is written next to it with a `_<target>` suffix. "
        "Pitfalls: the `libre` service needs a reachable `engine_url` (default "
        "http://localhost:5000); the `google` service requires the deep-translator "
        "package; MyMemory limits each request to ~500 characters (long texts are "
        "chunked automatically)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["translate", "detect", "languages"],
                "description": "Translation operation: translate (convert text to target_lang), detect (identify the source language), or languages (list supported language codes).",
            },
            "text": {"type": "string", "description": "Text to translate or detect; required for translate/detect unless file_path is given."},
            "source_lang": {"type": "string", "description": "Source language code (e.g. 'en', 'zh', 'fr'); defaults to 'auto' for auto-detection."},
            "target_lang": {"type": "string", "description": "Target language code for translate; defaults to 'en'."},
            "file_path": {"type": "string", "description": "Path to a text file to translate; the translated output is saved as '<stem>_<target_lang>.<ext>'. Alternative to passing text directly."},
            "service": {
                "type": "string",
                "enum": ["auto", "google", "mymemory", "libre"],
                "description": (
                    "Translation backend: 'auto' (default; tries google, then MyMemory, "
                    "then LibreTranslate), 'google' (deep-translator, keyless), 'mymemory' "
                    "(free MyMemory HTTP API, keyless, needs explicit source_lang for "
                    "detection), 'libre' (self-hosted LibreTranslate HTTP API for "
                    "privacy/offline)."
                ),
            },
            "engine_url": {"type": "string", "description": "Base URL of the LibreTranslate API server; libre service only. Defaults to 'http://localhost:5000'."},
            "preserve_format": {"type": "boolean", "description": "If true, treat input as HTML and preserve markup during translation; defaults to false (plain text)."},
        },
        "required": ["action"],
    },
    execute=_translation_execute,
    intents=["general", "research", "data"],
    category="communication",
    semantic_type="transform",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_readonly=lambda _: True,
)
