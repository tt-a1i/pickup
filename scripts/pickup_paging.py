"""Bounded, resumable output for a deterministic decoded session view.

``render_page`` returns no terminal newline. Its budget includes the one newline
added by the caller's single ``print``. Cursors are local continuation data, not
authentication tokens. The caller must fingerprint all decoded turns and notes.
"""

from __future__ import annotations

import base64
import json
import math
import re
import shlex
from typing import Any


DEFAULT_MAX_BYTES = 8000
MIN_MAX_CHARS = 1024
CURSOR_VERSION = 1
_FIELDS = {"version", "session_id", "fingerprint", "operation", "options", "offset"}


def _json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _validate(data: Any) -> dict:
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise ValueError("Invalid cursor fields; restart the selected view.")
    if type(data["version"]) is not int or data["version"] != CURSOR_VERSION:
        raise ValueError("Unsupported cursor version; restart the selected view.")
    if any(not isinstance(data[key], str) or not data[key] for key in ("session_id", "fingerprint")):
        raise ValueError("Invalid cursor identity; restart the selected view.")
    if data["operation"] not in ("show", "read"):
        raise ValueError("Invalid cursor operation; restart the selected view.")
    if not isinstance(data["options"], dict) or not _json_value(data["options"]):
        raise ValueError("Invalid cursor options; restart the selected view.")
    if type(data["offset"]) is not int or data["offset"] < 0:
        raise ValueError("Invalid cursor offset; restart the selected view.")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cursor field; restart the selected view.")
        result[key] = value
    return result


def decode_cursor(token: str) -> dict:
    """Decode and strictly validate cursor structure before CLI option recovery."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid cursor encoding; restart the selected view.")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return _validate(data)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"Invalid cursor; restart the selected view. ({error})") from None


def _encode(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _view_flags(operation, options):
    if operation == "show":
        flags = ["--tail", str(options["tail"])] if "tail" in options else []
        if options.get("all_branches"):
            flags.append("--all-branches")
        return flags
    flags = []
    for key in ("all", "turns", "grep", "with_tools", "all_branches"):
        value = options.get(key)
        if not value:
            continue
        flag = "--" + key.replace("_", "-")
        if value is True:
            flags.append(flag)
        elif str(value).startswith("-"):
            flags.append(f"{flag}={value}")
        else:
            flags.extend((flag, str(value)))
    return flags


def _budget_flags(max_chars, max_bytes):
    flags = []
    if max_chars is not None:
        flags.extend(("--max-chars", str(max_chars)))
    if max_bytes is not None:
        flags.extend(("--max-bytes", str(max_bytes)))
    return flags


def changed_session_error(previous, session_id, prefix, max_chars, max_bytes, *, options=None):
    if previous["session_id"] != session_id:
        return ValueError("Cursor belongs to another session; restart the selected view.")
    operation = previous["operation"]
    restart = shlex.join([*prefix, operation, session_id,
                          *_view_flags(operation, previous["options"] if options is None else options),
                          *_budget_flags(max_chars, max_bytes)])
    return ValueError(f"Session changed since the previous page.\nRestart with: {restart}")


def render_page(
    content: str,
    session_id: str,
    fingerprint: str,
    operation: str,
    options: dict,
    max_chars: int | None = None,
    cursor: str | None = None,
    command_prefix: list[str] | None = None,
    *,
    max_bytes: int | None = None,
) -> str:
    """Return one bounded page, or raise ``ValueError`` without partial output.

    All offsets count Python Unicode characters in ``content``. Session changes
    invalidate continuation even if they are outside the selected view. Page size
    and command prefix may change between calls; identity, view options and the
    session fingerprint may not. A complete page exhausts only the selected view.
    """
    if max_chars is not None and (type(max_chars) is not int or max_chars < MIN_MAX_CHARS):
        raise ValueError(f"--max-chars must be at least {MIN_MAX_CHARS}.")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("--max-bytes must be positive.")
    budget = _budget_flags(max_chars, max_bytes)
    byte_limit = max_bytes if max_bytes is not None else (max_chars * 4 if max_chars else DEFAULT_MAX_BYTES)
    char_limit = max_chars if max_chars is not None else byte_limit
    if not isinstance(content, str):
        raise ValueError("Page content must be text.")
    prefix = ["pickup"] if command_prefix is None else command_prefix
    if not isinstance(prefix, list) or not prefix or any(not isinstance(item, str) or not item for item in prefix):
        raise ValueError("command_prefix must be a nonempty list of command arguments.")
    state = _validate(dict(version=CURSOR_VERSION, session_id=session_id, fingerprint=fingerprint,
                           operation=operation, options=options, offset=0))
    start = 0
    if cursor is not None:
        previous = decode_cursor(cursor)
        if previous["session_id"] != session_id:
            raise ValueError("Cursor belongs to another session; restart the selected view.")
        if previous["fingerprint"] != fingerprint:
            raise changed_session_error(previous, session_id, prefix, max_chars, max_bytes)
        if previous["operation"] != operation or previous["options"] != options:
            raise ValueError("Cursor view options differ; restart the selected view.")
        start = previous["offset"]
        if start >= len(content):
            raise ValueError("Cursor offset is outside this view; restart the selected view.")

    view = "overview" if operation == "show" else "grep" if options.get("grep") else "dialogue"

    def assemble(end: int) -> str:
        complete = end == len(content)
        footer = f"view={view} complete={'yes' if complete else 'no'} (selected decoded view)"
        if complete:
            footer += "\nEnd of selected view."
        else:
            token = _encode(dict(state, offset=end))
            footer += "\nNext: " + shlex.join([*prefix, operation, session_id, *budget, "--cursor", token])
        return (f"# Pickup page: characters {start}:{end}/{len(content)}\n\n"
                + content[start:end] + "\n\n---\n" + footer)

    def fits(page):
        return len(page) + 1 <= char_limit and len(page.encode("utf-8")) + 1 <= byte_limit

    # The completion footer is much shorter than a continuation command. Try it
    # first, then find a partial page using the actual metadata size.
    if len(content) - start <= min(char_limit, byte_limit):
        final = assemble(len(content))
        if fits(final):
            return final
    low, high = start, min(len(content) - 1, start + char_limit, start + byte_limit)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(assemble(middle)):
            low = middle
        else:
            high = middle - 1
    if low <= start:
        raise ValueError("Page budget cannot fit metadata and content; increase --max-bytes or --max-chars, or shorten view options.")
    # Prefer whole lines without sacrificing progress through a huge first line.
    newline = content.rfind("\n", start, low)
    end = newline + 1 if newline >= start else low
    result = assemble(end)
    return result
