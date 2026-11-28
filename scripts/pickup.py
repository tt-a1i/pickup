#!/usr/bin/env python3
"""
pickup — Cross-agent local session finder, outline extractor, and full/range reader.

Supports macOS local stores out of the box (read-only, zero index DB, zero daemon):
  - Antigravity (~/.gemini/antigravity)
  - Codex       (~/.codex/sessions, ~/.codex/archived_sessions)
  - Claude Code (~/.claude/projects)
  - Qoder       (~/.qoder/projects, ~/.qoder-cn/projects, ~/.qoderwork/projects)
  - Pi          (~/.pi/agent/sessions)
  - Kimi Code   (~/.kimi-code/sessions)
  - Devin CLI   (~/.local/share/devin/cli/sessions.db)
"""

from __future__ import annotations

import argparse
import hashlib
from contextlib import closing, contextmanager
import datetime as dt
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

import pickup_extra
import pickup_pi
import pickup_paging
from pickup_scan import QueryPrefilter, ScanReport

try:
    from compression import zstd
except ImportError:
    zstd = None

HOME = pathlib.Path.home()

# Codex injected system blocks that appear under role="user"
CODEX_SYSTEM_PREFIXES = (
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<app-context>",
    "# Files mentioned by the user:",
    "<subagent_notification>",
    "<codex_internal_context",
)


@dataclass
class Turn:
    index: int
    timestamp: str
    user_text: str
    assistant_texts: list[str] = field(default_factory=list)
    tool_summaries: list[str] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    off_branch: bool = False
    inherited_prompt: bool = False


@dataclass
class SessionMeta:
    uid: str              # e.g. "antigravity:942f0a64"
    agent: str            # antigravity | codex | claude | qoder | pi
    raw_id: str
    path: pathlib.Path
    mtime: float
    cwd: str = ""
    title: str = ""
    status: str = ""
    preview: str = ""
    match_evidence: str = ""
    parse_warnings: int = 0
    location: str = ""
    record_variant: bool = False
    metadata_only: bool = False
    match_turn: int = 0
    models: set[str] = field(default_factory=set)
    is_subagent: bool = False
    fidelity: str = ""
    stored_model: str = ""
    match_off_branch: bool = False
    indexed_chunks: list[tuple[Any, ...]] | None = field(default=None, repr=False)


SOURCE_FIDELITY = {
    "indexed_text": "Antigravity SQLite indexed text (conversations_fts)",
    "sqlite_history": "Codex SQLite projected dialogue (thread_items)",
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def fmt_ts(epoch_or_iso: float | str | None) -> str:
    if epoch_or_iso is None or epoch_or_iso == "":
        return ""
    if isinstance(epoch_or_iso, (int, float)):
        try:
            timestamp = epoch_or_iso / 1000 if abs(epoch_or_iso) > 100_000_000_000 else epoch_or_iso
            return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return ""
    s = str(epoch_or_iso).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16]


def one_line(text: str, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def truncate_block(text: str, limit: int = 2400) -> str:
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n… [truncated {len(s) - limit} chars]"


def match_excerpt(text: str, keyword: str, context: int = 200) -> str:
    index = text.lower().find(keyword.lower())
    if index < 0:
        return truncate_block(text, context * 2)
    start = max(0, index - context)
    end = min(len(text), index + len(keyword) + context)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def clean_antigravity_user_text(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", raw)
    text = m.group(1) if m else raw
    # Strip @[Quote] boilerplate if followed by real text, keep concise
    return text.strip()


def caller_session_ids() -> set[str]:
    """Use explicit runtime identities; prompt wording cannot identify the caller."""
    # Claude supplies the local ID to hooks as session_id. A caller can pass
    # it through CLAUDE_SESSION_ID. Remote control IDs identify web sessions.
    keys = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDE_SESSION_ID",
            "KIMI_SESSION_ID", "DEVIN_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID")
    return {os.environ[k] for k in keys if os.environ.get(k)}


def is_excluded(meta: SessionMeta, excluded: set[str]) -> bool:
    return meta.raw_id in excluded or meta.uid in excluded


def jsonl_lines(path):
    # Unicode line separators inside a JSON string are part of the record.
    with path.open(encoding="utf-8", errors="replace") as stream:
        yield from stream


# ---------------------------------------------------------------------------
# Store 1: Antigravity (~/.gemini/antigravity)
# ---------------------------------------------------------------------------

def discover_antigravity(on_error=None, *, include_subagents=False, batch_indexed_text=False) -> list[SessionMeta]:
    base = HOME / ".gemini" / "antigravity"
    brain = base / "brain"
    if not base.is_dir():
        return []

    sessions: dict[str, SessionMeta] = {}
    subagents: set[str] = set()
    db_path = base / "conversation_summaries.db"
    if db_path.is_file():
        try:
            with closing(pickup_extra.open_readonly_db(db_path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
                modified_column = "last_modified_time" if "last_modified_time" in columns else "NULL"
                rows = conn.execute(
                    "SELECT conversation_id, title, workspace_uris, status, parent_conversation_id, "
                    f"{modified_column} FROM conversation_summaries"
                ).fetchall()
            for cid, title, ws_raw, status, parent_id, modified in rows:
                if not cid:
                    continue
                if parent_id and not include_subagents:
                    subagents.add(cid)
                    continue
                workspace = re.search(r"file://([^\s\"'\],]+)", str(ws_raw or ""))
                try:
                    mtime = dt.datetime.fromisoformat(str(modified).replace("Z", "+00:00")).timestamp()
                except (ValueError, OSError, OverflowError):
                    mtime = 0
                sessions[cid] = SessionMeta(
                    f"antigravity:{cid[:8]}", "antigravity", cid, db_path, mtime,
                    cwd=workspace[1] if workspace else "", title=title or "",
                    status=(status or "").replace("CASCADE_RUN_STATUS_", ""), metadata_only=True,
                    is_subagent=bool(parent_id),
                )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            if on_error:
                on_error("antigravity", "metadata unavailable; falling back to transcript files")

    for d in brain.iterdir() if brain.is_dir() else ():
        cid = d.name
        if cid in subagents:
            continue
        logs = d / ".system_generated" / "logs"
        full = logs / "transcript_full.jsonl"
        tp = full if full.is_file() else logs / "transcript.jsonl"
        if not tp.is_file():
            continue
        mtime = tp.stat().st_mtime
        if cid in sessions:
            meta = sessions[cid]
            meta.path = tp
            meta.mtime = mtime
            meta.metadata_only = False
        else:
            sessions[cid] = SessionMeta(f"antigravity:{cid[:8]}", "antigravity", cid, tp, mtime)
    missing = {cid for cid, meta in sessions.items() if meta.metadata_only}
    if missing:
        try:
            indexed = pickup_extra.antigravity_indexed_sessions(db_path, missing, batch_chunks=batch_indexed_text)
            for cid, chunks in indexed.items():
                meta = sessions[cid]
                meta.metadata_only = False
                meta.fidelity = "indexed_text"
                meta.indexed_chunks = chunks
        except (OSError, sqlite3.Error):
            if on_error:
                on_error("antigravity", "indexed text unavailable; retaining session metadata")
    return list(sessions.values())


def parse_antigravity_turns(meta: SessionMeta, use_full: bool = True) -> tuple[list[Turn], list[str]]:
    tp = meta.path
    if use_full:
        full_p = tp.parent / "transcript_full.jsonl"
        if full_p.is_file():
            tp = full_p

    turns: list[Turn] = []
    current: Turn | None = None

    for line in jsonl_lines(tp):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            meta.parse_warnings += 1
            continue
        if not isinstance(obj, dict):
            meta.parse_warnings += 1
            continue
        stype = obj.get("type")
        created = fmt_ts(obj.get("created_at"))

        if stype == "USER_INPUT":
            utext = clean_antigravity_user_text(str(obj.get("content") or ""))
            if not utext:
                continue
            current = Turn(index=len(turns) + 1, timestamp=created, user_text=utext)
            turns.append(current)
        elif stype == "PLANNER_RESPONSE":
            if isinstance(obj.get("model"), str) and obj["model"]:
                meta.models.add(obj["model"])
            if current is None:
                current = Turn(index=len(turns) + 1, timestamp=created, user_text="(initial)")
                turns.append(current)
            content = str(obj.get("content") or "").strip()
            if content:
                current.assistant_texts.append(content)
            for tc in obj.get("tool_calls") or []:
                tname = str(tc.get("name") or tc.get("tool_name") or "tool").replace("default_api:", "")
                args = tc.get("args") or tc.get("arguments") or {}
                detail = ""
                if isinstance(args, dict):
                    detail = (
                        args.get("CommandLine")
                        or args.get("Path")
                        or args.get("query")
                        or args.get("toolSummary")
                        or ""
                    )
                current.tool_summaries.append(f"{tname}({one_line(str(detail), 90)})" if detail else tname)
        elif stype in ("GENERIC", "SYSTEM_MESSAGE"):
            if current is not None:
                c = str(obj.get("content") or "").strip()
                if c:
                    current.tool_outputs.append(c)
                if obj.get("status") == "ERROR":
                    current.errors.append(one_line(c, 200))
        elif stype == "ERROR_MESSAGE":
            if current is not None:
                err = str(obj.get("error") or obj.get("content") or "ERROR_MESSAGE")
                current.errors.append(one_line(err, 200))

    # Check recent background task logs in Antigravity brain/<id>/.system_generated/tasks/
    extra_notes: list[str] = []
    tasks_dir = meta.path.parent.parent / "tasks"
    if tasks_dir.is_dir():
        tfiles = sorted(tasks_dir.glob("task-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        for tf in tfiles:
            st = tf.stat()
            if meta.mtime - st.st_mtime < 3600:
                with tf.open("rb") as stream:
                    stream.seek(max(0, st.st_size - 600))
                    tail_txt = stream.read(600).decode("utf-8", errors="ignore").strip()
                extra_notes.append(
                    f"Background `{tf.name}` (updated {fmt_ts(st.st_mtime)}, {st.st_size}B): {one_line(tail_txt, 160)}"
                )
    return turns, extra_notes


# ---------------------------------------------------------------------------
# Store 2: Codex (~/.codex/sessions, ~/.codex/archived_sessions)
# ---------------------------------------------------------------------------

def _is_codex_injected_user_block(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    if re.fullmatch(r"<skill>\s*<name>[^<]+</name>[\s\S]*</skill>", s):
        return True
    if s.startswith(CODEX_SYSTEM_PREFIXES):
        return True
    if s.startswith("<image ") or s == "</image>":
        return True
    return False


def codex_user_text(text: str) -> str:
    text = text.strip()
    if text.startswith("# Files mentioned by the user:"):
        request = re.search(r"^## My request:[ \t]*", text, re.MULTILINE)
        return text[request.end():].strip() if request else ""
    return "" if _is_codex_injected_user_block(text) else text


def codex_user_parts(content):
    text = []
    image_paths = []
    image_count = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "input_image":
            image_count += 1
            continue
        raw = str(part.get("text") or "").strip()
        if raw.startswith("<image "):
            path = re.search(r'\bpath="(/[^"\r\n]*)"', raw)
            image_paths.append(path[1] if path else "")
            continue
        cleaned = codex_user_text(raw)
        if cleaned:
            text.append(cleaned)
    for path in image_paths:
        text.append(f"[Image attachment: {path}]" if path else "[Image attachment]")
    text.extend(["[Image attachment]"] * max(0, image_count - len(image_paths)))
    return "\n".join(text)


@contextmanager
def open_codex_log(path, *, header_only=False, binary=False):
    mode = "rb" if binary else "rt"
    encoding = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    if path.suffix != ".zst":
        with path.open(mode, **encoding) as stream:
            yield stream
        return
    if zstd is not None:
        try:
            with zstd.open(path, mode, **encoding) as stream:
                yield stream
        except (zstd.ZstdError, EOFError) as error:
            raise OSError("Invalid Zstandard history") from error
        return
    # Used on Python versions before 3.14. Closing a header-only read stops
    # the decoder without consuming the entire historical conversation.
    with subprocess.Popen(["zstd", "-qdc", "--", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        with (process.stdout if binary else io.TextIOWrapper(process.stdout, **encoding)) as stream:
            yield stream
        if process.wait() and not header_only:
            raise OSError("Invalid Zstandard history")


def codex_roots():
    return [HOME / ".codex" / store for store in ("sessions", "archived_sessions")]


def discover_codex(on_error=None, *, include_subagents=False) -> list[SessionMeta]:
    out: list[SessionMeta] = []
    file_ids = set()
    files = (path for root in codex_roots() for path in root.rglob("*.jsonl*")
             if path.name.endswith((".jsonl", ".jsonl.zst")))
    has_zstd = zstd is not None or shutil.which("zstd") is not None
    skipped_compressed = False
    for f in files:
        if f.suffix == ".zst" and not has_zstd:
            skipped_compressed = True
            continue
        # Fast header peek (first 25 lines) for session_meta & subagent check
        sid = f.name.removesuffix(".zst").removesuffix(".jsonl")
        cwd = ""
        is_subagent = False
        try:
            st = f.stat()
            with open_codex_log(f, header_only=True) as fh:
                for _ in range(25):
                    line = fh.readline()
                    if not line:
                        break
                    if '"session_meta"' in line:
                        o = json.loads(line)
                        p = o.get("payload") or {}
                        sid = p.get("id") or sid
                        cwd = p.get("cwd") or ""
                        src = p.get("source")
                        if isinstance(src, dict) and "subagent" in src:
                            is_subagent = True
                        break
        except (OSError, ValueError, TypeError, AttributeError):
            if on_error:
                on_error("codex", "session metadata could not be read")
            continue

        file_ids.add(sid)
        if is_subagent and not include_subagents:
            continue

        out.append(
            SessionMeta(
                uid=f"codex:{sid[-8:]}" if len(sid) >= 8 else f"codex:{sid}",
                agent="codex",
                raw_id=sid,
                path=f,
                mtime=st.st_mtime,
                cwd=cwd,
                location=f.relative_to(HOME / ".codex").parts[0],
                is_subagent=is_subagent,
            )
        )
    if skipped_compressed and on_error:
        on_error("codex", "compressed records require Python 3.14 or the zstd command")
    base = HOME / ".codex"
    state_files = sorted((p for p in base.glob("state_*.sqlite") if p.stem[6:].isdigit()),
                         key=lambda p: int(p.stem[6:]), reverse=True)
    try:
        rows = pickup_extra.discover_codex_history(
            base / "thread_history_1.sqlite", next(iter(state_files), None), file_ids, on_error=on_error,
        )
        out.extend(SessionMeta(uid=f"codex:{row['raw_id'][-8:]}", agent="codex", location="sqlite",
                               fidelity="sqlite_history", **row)
                   for row in rows if include_subagents or not row["is_subagent"])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        if on_error:
            on_error("codex", "SQLite history unavailable; falling back to rollout files")
    return out


# Large Codex compaction snapshots and duplicate presentation events don't
# contribute to search. Recognize the writer's canonical header and defer all
# other record shapes to the regular decoder.
CODEX_NON_SEARCH_RECORD = re.compile(
    r'^\{"timestamp":"[^"\\]+","(?:ordinal":\d+,")?type":'
    r'(?:"(?:compacted|event_msg|token_usage_record|world_state)","payload":\{'
    r'|"response_item","payload":\{"type":"reasoning",)'
)
CODEX_NON_SEARCH_BYTES = re.compile(CODEX_NON_SEARCH_RECORD.pattern.encode("ascii"))
CODEX_TOOL_BYTES = re.compile(
    rb'^\{"timestamp":"[^"\\]+","(?:ordinal":\d+,")?type":"response_item","payload":'
    rb'\{"type":"(?:function_call|custom_tool_call)(?:_output)?",'
)


def parse_codex_turns(meta: SessionMeta, *, search_only=False, prefilter=None) -> tuple[list[Turn], list[str]]:
    with open_codex_log(meta.path, binary=search_only) as stream:
        lines = jsonl_byte_lines(stream) if search_only else stream
        return parse_codex_records(meta, lines, search_only=search_only, prefilter=prefilter)


def jsonl_byte_lines(stream):
    # Match the text reader's support for CRLF and older CR line boundaries.
    for line in stream:
        if b"\r" in line:
            yield from line.splitlines()
        else:
            yield line


def parse_codex_records(meta: SessionMeta, lines, *, search_only=False, prefilter=None) -> tuple[list[Turn], list[str]]:
    turns: list[Turn] = []
    current: Turn | None = None
    for line in lines:
        if isinstance(line, bytes):
            if search_only and line.endswith((b"}\n", b"}\r\n", b"}")):
                if CODEX_NON_SEARCH_BYTES.match(line):
                    continue
                if (prefilter and prefilter.prune_tools and CODEX_TOOL_BYTES.match(line)
                        and not prefilter.line_may_match(line)
                        and b'"message"' not in line and b'"turn_context"' not in line):
                    continue
            line = line.decode("utf-8", errors="replace")
        elif search_only and line.endswith(("}\n", "}")) and CODEX_NON_SEARCH_RECORD.match(line):
            continue
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            meta.parse_warnings += 1
            continue

        if not isinstance(obj, dict):
            meta.parse_warnings += 1
            continue
        otype = obj.get("type")
        p = obj.get("payload") or {}
        if not isinstance(p, dict):
            continue

        if otype == "turn_context":
            if not meta.cwd and p.get("cwd"):
                meta.cwd = str(p["cwd"])
            if isinstance(p.get("model"), str) and p["model"]:
                meta.models.add(p["model"])

        if otype != "response_item":
            continue

        ptype = p.get("type")
        role = p.get("role")

        if ptype == "message" and role == "user":
            utext = codex_user_parts(p.get("content") or [])
            if not utext:
                continue
            current = Turn(index=len(turns) + 1, timestamp=fmt_ts(obj.get("timestamp")), user_text=utext)
            turns.append(current)

        elif ptype == "message" and role == "assistant":
            if current is None:
                current = Turn(index=len(turns) + 1, timestamp=fmt_ts(obj.get("timestamp")), user_text="(initial)")
                turns.append(current)
            aparts = [
                str(b.get("text") or "").strip()
                for b in (p.get("content") or [])
                if isinstance(b, dict) and b.get("text")
            ]
            atext = "\n".join(x for x in aparts if x)
            if atext:
                current.assistant_texts.append(atext)

        elif ptype in ("function_call", "custom_tool_call"):
            if current is not None:
                name = str(p.get("name") or "tool")
                raw_args = str(p.get("arguments") or p.get("input") or "")
                current.tool_summaries.append(f"{name}({one_line(raw_args, 90)})" if raw_args else name)

        elif ptype in ("function_call_output", "custom_tool_call_output"):
            if current is not None:
                out_txt = str(p.get("output") or "").strip()
                if out_txt:
                    current.tool_outputs.append(out_txt)

    return turns, []


# ---------------------------------------------------------------------------
# Store 3 & 4: Claude Code (~/.claude) & Qoder (~/.qoder, ~/.qoder-cn, ~/.qoderwork)
# ---------------------------------------------------------------------------

def discover_claude_like(agent_name: str, roots: list[pathlib.Path], *, include_subagents=False) -> list[SessionMeta]:
    candidates: list[pathlib.Path] = []
    for r in roots:
        if r.is_dir():
            candidates.extend(r.glob("*/*.jsonl"))
            if include_subagents:
                candidates.extend(r.glob("*/*/subagents/*.jsonl"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: list[SessionMeta] = []
    for f in candidates:
        is_subagent = f.parent.name == "subagents" or f.name.startswith("agent-")
        if is_subagent and not include_subagents:
            continue
        st = f.stat()
        sid = f.stem
        out.append(
            SessionMeta(
                uid=f"{agent_name}:{sid if is_subagent else sid[:8]}",
                agent=agent_name,
                raw_id=sid,
                path=f,
                mtime=st.st_mtime,
                is_subagent=is_subagent,
            )
        )
    return out


def claude_like_records(meta):
    for line in jsonl_lines(meta.path):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            meta.parse_warnings += 1
            continue

        if not isinstance(obj, dict):
            meta.parse_warnings += 1
            continue
        if not meta.cwd:
            if obj.get("cwd"):
                meta.cwd = str(obj["cwd"])
            elif obj.get("type") == "workspace-directories" and obj.get("directories"):
                meta.cwd = str(obj["directories"][0])
        yield obj


def claude_user_text(obj):
    if obj.get("type") != "user" or obj.get("isMeta"):
        return ""
    msg = obj.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else ""
    if isinstance(content, str):
        text = content.strip()
        return "" if text.startswith("<local-command-") else text
    if isinstance(content, list):
        parts = [str(b["text"]).strip() for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return "\n".join(part for part in parts if part)
    return ""


def parse_claude_like_turns(meta, *, all_branches=False):
    records = claude_like_records(meta)
    if meta.agent != "qoder":
        return decode_claude_like(meta, records), []
    records = list(records)
    graph = pickup_extra.qoder_branches(records, all_branches=all_branches)
    if graph is None:
        return decode_claude_like(meta, records), []
    paths, incomplete = graph
    meta.parse_warnings += int(incomplete)
    prompts, first_dialogue = {}, None
    for record in records:
        text = claude_user_text(record)
        if text:
            first_dialogue = first_dialogue or "user"
            prompts[id(record)] = (len(prompts) + 1 + (first_dialogue == "assistant"), text)
        elif record.get("type") == "assistant":
            first_dialogue = first_dialogue or "assistant"
    # Preserve the original turn numbers across views. Rewound replies can
    # share a number with the active reply to the same prompt.
    contexts = {}
    turns = []
    for branch, (parent, records) in enumerate(paths):
        turns.extend(decode_claude_like(meta, records, prompts=prompts, contexts=contexts,
                                       inherited=contexts.get(parent), off_branch=bool(branch),
                                       initial_index=1 if first_dialogue == "assistant" else len(prompts) + 1))
    if all_branches:
        turns.sort(key=lambda turn: turn.index)
    return turns, []


def decode_claude_like(meta, records, *, prompts=None, contexts=None, inherited=None, off_branch=False,
                       initial_index=None):
    turns = []
    current = None
    seen_assistant_chunks = set()

    def new_turn(text, timestamp, index=None):
        turn = Turn(index if index is not None else len(turns) + 1, timestamp, text, off_branch=off_branch)
        turns.append(turn)
        return turn

    def reply_turn(timestamp):
        if current is not None:
            return current
        if inherited is not None:
            turn = new_turn(inherited.user_text, inherited.timestamp, inherited.index)
            turn.inherited_prompt = True
            return turn
        return new_turn("(initial)", timestamp, initial_index)

    for obj in records:
        otype = obj.get("type")
        ts = fmt_ts(obj.get("timestamp"))
        uuid = obj.get("uuid")

        if otype == "user" and not obj.get("isMeta"):
            msg = obj.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else ""
            if isinstance(content, list) and (current is not None or inherited is not None):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        rc = b.get("content")
                        if isinstance(rc, list):
                            rc = "\n".join(part["text"] for part in rc
                                           if isinstance(part, dict) and part.get("type") == "text"
                                           and isinstance(part.get("text"), str))
                        if isinstance(rc, str) and rc.strip():
                            current = reply_turn(ts)
                            current.tool_outputs.append(rc.strip())
            index, text = prompts.get(id(obj), (None, "")) if prompts is not None else (None, claude_user_text(obj))
            if text:
                current = new_turn(text, ts, index)

        elif otype == "assistant":
            current = reply_turn(ts)
            msg = obj.get("message") or {}
            msg = msg if isinstance(msg, dict) else {}
            if isinstance(msg.get("model"), str) and msg["model"]:
                meta.models.add(msg["model"])
            mid = str(msg.get("id") or "")
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text" and b.get("text"):
                    txt = str(b["text"]).strip()
                    sig = (mid, txt)
                    if txt and sig not in seen_assistant_chunks:
                        seen_assistant_chunks.add(sig)
                        current.assistant_texts.append(txt)
                elif btype == "tool_use":
                    tname = str(b.get("name") or "tool")
                    tinp = b.get("input") or {}
                    sig = (mid, f"tool:{b.get('id') or tname}:{ json.dumps(tinp, ensure_ascii=False)[:80] }")
                    if sig not in seen_assistant_chunks:
                        seen_assistant_chunks.add(sig)
                        detail = ""
                        if isinstance(tinp, dict):
                            detail = (
                                tinp.get("command")
                                or tinp.get("file_path")
                                or tinp.get("path")
                                or tinp.get("pattern")
                                or ""
                            )
                        current.tool_summaries.append(f"{tname}({one_line(str(detail), 90)})" if detail else tname)

        if contexts is not None and isinstance(uuid, str):
            contexts[uuid] = current if current is not None else inherited

    return turns


# ---------------------------------------------------------------------------
# Store 5: Pi (~/.pi/agent/sessions)
# ---------------------------------------------------------------------------

def discover_pi() -> list[SessionMeta]:
    root = HOME / ".pi" / "agent" / "sessions"
    if not root.is_dir():
        return []
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[SessionMeta] = []
    for f in files:
        st = f.stat()
        sid = f.stem.split("_")[-1]
        try:
            with f.open(encoding="utf-8", errors="replace") as stream:
                header = json.loads(stream.readline())
            if isinstance(header, dict) and header.get("type") == "session" and header.get("id"):
                sid = str(header["id"])
        except (OSError, ValueError):
            pass
        out.append(
            SessionMeta(
                uid=f"pi:{sid}",
                agent="pi",
                raw_id=sid,
                path=f,
                mtime=st.st_mtime,
            )
        )
    return out


def parse_pi_turns(meta: SessionMeta) -> tuple[list[Turn], list[str]]:
    turns: list[Turn] = []
    current: Turn | None = None
    entries = []
    for line in jsonl_lines(meta.path):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            meta.parse_warnings += 1
            continue
        if isinstance(obj, dict):
            entries.append(obj)
        else:
            meta.parse_warnings += 1
    entries, notes = pickup_pi.select_entries(entries)
    for obj in entries:
        if not isinstance(obj, dict):
            meta.parse_warnings += 1
            continue
        otype = obj.get("type")
        ts = fmt_ts(obj.get("timestamp"))
        if otype == "session" and obj.get("cwd"):
            meta.cwd = str(obj["cwd"])
        elif otype == "message":
            msg = obj.get("message") or {}
            role = msg.get("role")
            content = msg.get("content") or []
            if role == "user" and isinstance(content, list):
                utext = "\n".join(str(b.get("text") or "").strip() for b in content if isinstance(b, dict) and b.get("text")).strip()
                if utext:
                    current = Turn(index=len(turns) + 1, timestamp=ts, user_text=utext)
                    turns.append(current)
            elif role == "assistant" and isinstance(content, list):
                if current is None:
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        current.assistant_texts.append(str(b["text"]).strip())
                    elif b.get("type") == "toolCall":
                        current.tool_summaries.append(str(b.get("name") or "tool"))
            elif role == "toolResult" and current is not None:
                if isinstance(content, str):
                    text = content.strip()
                else:
                    text = "\n".join(str(part["text"]) for part in content
                                     if isinstance(part, dict) and part.get("type") == "text"
                                     and part.get("text")).strip()
                if text:
                    current.tool_outputs.append(text)
                    if msg.get("isError"):
                        current.errors.append(text)
    meta.status = ""
    if not turns and notes and all(entry.get("type") == "session" for entry in entries):
        meta.status = "UNREADABLE"
        print(f"Cannot read {meta.uid}: {'; '.join(notes)}", file=sys.stderr)
    return turns, notes


# ---------------------------------------------------------------------------
# Unified discovery, parsing, matching, and rendering
# ---------------------------------------------------------------------------

AGENTS = ("antigravity", "codex", "claude", "qoder", "pi", "kimi", "devin")


def discover_all(agents=None, report=None, *, include_subagents=False, batch_indexed_text=False) -> list[SessionMeta]:
    selected = AGENTS if agents is None else agents
    roots = {
        "antigravity": [HOME / ".gemini/antigravity/brain", HOME / ".gemini/antigravity/conversation_summaries.db"],
        "codex": [*codex_roots(), HOME / ".codex/thread_history_1.sqlite"],
        "claude": [HOME / ".claude/projects"],
        "qoder": [HOME / ".qoder/projects", HOME / ".qoder-cn/projects", HOME / ".qoderwork/projects"],
        "pi": [HOME / ".pi/agent/sessions"],
        "kimi": [HOME / ".kimi-code/sessions"],
        "devin": [HOME / ".local/share/devin/cli/sessions.db"],
    }
    def diagnostic(agent, reason):
        if report:
            report.discovery_failed(agent, reason)
        else:
            print(f"Discovery failed [{agent}]: {reason}", file=sys.stderr)
    loaders = {
        "antigravity": lambda: discover_antigravity(
            on_error=(lambda ag, why: report.warning(ag, why)) if report else None,
            include_subagents=include_subagents,
            batch_indexed_text=batch_indexed_text,
        ),
        "codex": lambda: discover_codex(on_error=diagnostic, include_subagents=include_subagents),
        "claude": lambda: discover_claude_like("claude", roots["claude"], include_subagents=include_subagents),
        "qoder": lambda: discover_claude_like("qoder", roots["qoder"], include_subagents=include_subagents),
        "pi": discover_pi,
        "kimi": lambda: pickup_extra.discover_kimi(roots["kimi"][0], on_error=diagnostic),
        "devin": lambda: pickup_extra.discover_devin(roots["devin"][0], on_error=diagnostic),
    }
    items = []
    for agent in selected:
        if report:
            report.source(agent, any(path.exists() for path in roots[agent]))
            print(f"Discovering {agent} stores...", file=report.stream, flush=True)
        try:
            found = loaders[agent]()
            if agent in ("kimi", "devin"):
                found = [SessionMeta(uid=f"{agent}:{data['raw_id']}", agent=agent,
                         raw_id=data["raw_id"], path=pathlib.Path(data["path"]),
                         mtime=data["mtime"], cwd=data["cwd"], title=data["title"]) for data in found]
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            diagnostic(agent, "source discovery failed; this source may be incomplete")
            found = []
        if report:
            report.discovered(agent, len(found))
        items.extend(found)
    # Expand short ID collisions first. Same-ID records need a source selector.
    counts: dict[str, int] = {}
    for item in items:
        counts[item.uid] = counts.get(item.uid, 0) + 1
    for item in items:
        if counts[item.uid] > 1:
            item.uid = f"{item.agent}:{item.raw_id}"
    counts.clear()
    for item in items:
        counts[item.uid] = counts.get(item.uid, 0) + 1
    for item in items:
        if counts[item.uid] > 1:
            path = item.path
            if path.is_relative_to(HOME):
                path = path.relative_to(HOME)
            record_key = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
            item.uid = f"{item.uid}@{record_key}"
            item.record_variant = True
    items.sort(key=lambda m: m.mtime, reverse=True)
    return items


def parse_session(meta: SessionMeta, use_full: bool = True, *, search_only=False,
                  all_branches=False, prefilter=None) -> tuple[list[Turn], list[str]]:
    meta.parse_warnings = 0
    meta.models.clear()
    if meta.status == "UNREADABLE":
        meta.status = ""
    try:
        result = _parse_session(meta, use_full=use_full, search_only=search_only,
                                all_branches=all_branches, prefilter=prefilter)
        if meta.parse_warnings:
            print(f"Decode warning [{meta.uid}]: {meta.parse_warnings} malformed records or incomplete branches; decoded view may be incomplete", file=sys.stderr)
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        meta.status = "UNREADABLE"
        print(f"Cannot read {meta.uid}: source read or format error", file=sys.stderr)
        return [], []


def _parse_session(meta: SessionMeta, use_full: bool = True, *, search_only=False,
                   all_branches=False, prefilter=None) -> tuple[list[Turn], list[str]]:
    if meta.fidelity:
        if meta.stored_model:
            meta.models.add(meta.stored_model)

        def decode_warning():
            meta.parse_warnings += 1

        messages = (pickup_extra.antigravity_indexed_messages(meta.path, meta.raw_id, chunks=meta.indexed_chunks)
                    if meta.fidelity == "indexed_text" else
                    pickup_extra.codex_history_messages(meta.path, meta.raw_id, on_error=decode_warning))
        turns = []
        for role, text, stamp in messages:
            text = codex_user_text(text) if role == "user" and meta.agent == "codex" else text.strip()
            if not text:
                continue
            if role == "user" or not turns:
                turns.append(Turn(len(turns) + 1, fmt_ts(stamp), text if role == "user" else "(initial)"))
            if role == "assistant":
                turns[-1].assistant_texts.append(text)
        return turns, []
    if meta.agent in ("kimi", "devin"):
        parser = pickup_extra.parse_kimi if meta.agent == "kimi" else pickup_extra.parse_devin
        def decode_warning(agent, reason):
            meta.parse_warnings += 1
        records = parser(str(meta.path), on_error=decode_warning)
        return [Turn(index=i, **record) for i, record in enumerate(records, 1)], []
    if meta.agent == "antigravity":
        return parse_antigravity_turns(meta, use_full=use_full)
    if meta.agent == "codex":
        return parse_codex_turns(meta, search_only=search_only, prefilter=prefilter)
    if meta.agent in ("claude", "qoder"):
        return parse_claude_like_turns(meta, all_branches=all_branches)
    if meta.agent == "pi":
        return parse_pi_turns(meta)
    return [], []


SESSION_LINK = re.compile(
    r"(?:(?P<codex>codex://threads/)|conversation://)"
    r"(?P<id>[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})/?",
    re.IGNORECASE,
)
FULL_SESSION_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.IGNORECASE)


def normalize_session_target(target: str) -> str:
    target = target.strip()
    # Chat clients often send a pasted deep link as Markdown.
    markdown_link = re.fullmatch(r"\[[^\]\n]*\]\(([^()\s]+)\)", target)
    link = SESSION_LINK.fullmatch(markdown_link[1] if markdown_link else target)
    if link:
        agent = "codex" if link["codex"] else "antigravity"
        return f"{agent}:{link['id'].lower()}"
    source, sep, value = target.partition(":")
    if sep and source.lower() in AGENTS:
        return f"{source.lower()}:{value}"
    return target


def matching_ids(all_metas: list[SessionMeta], target: str) -> list[SessionMeta]:
    exact = [m for m in all_metas if target in (m.uid, m.raw_id)]
    if exact:
        return exact
    agent, sep, fragment = target.partition(":")
    if not sep:
        fragment, agent = agent, ""
    if not fragment:
        return []
    return [m for m in all_metas if (not agent or m.agent == agent.lower())
            and (m.raw_id.startswith(fragment) or m.raw_id.endswith(fragment))]


def resolve_by_id(all_metas: list[SessionMeta], target: str) -> SessionMeta | None:
    matches = matching_ids(all_metas, normalize_session_target(target))
    if len(matches) > 1:
        records = [m for m in matches if m.record_variant]
        if records:
            choices = ", ".join(f"{m.uid} ({m.path.name})" for m in records)
            raise ValueError(f"Ambiguous session ID: {target}; select a record UID: {choices}")
        raise ValueError(f"Ambiguous session ID: {target}; use a full ID")
    return matches[0] if matches else None


def source_may_match(meta, prefilter):
    if not prefilter.anchor or meta.fidelity or meta.agent not in ("codex", "qoder", "claude"):
        return True
    metadata = "\n".join((meta.title, meta.cwd, meta.uid, meta.raw_id, meta.agent))
    if prefilter.required in metadata.lower():
        return True
    try:
        if meta.agent == "codex":
            if meta.path.suffix == ".zst" and zstd is None:
                return True  # The external decoder's exit status is checked during the full scan.
            with open_codex_log(meta.path, binary=True) as stream:
                return prefilter.may_match(stream, ignored=CODEX_NON_SEARCH_BYTES)
        with meta.path.open("rb") as stream:
            return prefilter.may_match(stream)
    except (OSError, ValueError):
        return True  # Let the regular decoder report the source error.


def find_matching_sessions(all_metas: list[SessionMeta], query: str | None, limit: int = 8,
                           excluded: set[str] | None = None, report=None, *, by_id=False,
                           project="", since=None, model="") -> list[tuple[SessionMeta, list[Turn], list[str]]]:
    q = (query or "").strip()
    q_low = q.lower()
    project = project.lower()
    model = model.lower()
    prefilter = QueryPrefilter(q_low) if q and not by_id else None

    results: list[tuple[int, SessionMeta, list[Turn], list[str]]] = []

    # Scan candidates in mtime order
    excluded = caller_session_ids() | (excluded or set())
    identity_matches = {id(m) for m in matching_ids(all_metas, q)} if by_id else set()
    for meta in all_metas:
        if (is_excluded(meta, excluded) or (since is not None and meta.mtime < since)
                or (project and meta.cwd and project not in meta.cwd.lower())):
            if report:
                report.excluded(meta.agent)
            continue
        if by_id and id(meta) not in identity_matches:
            continue
        if report:
            report.attempted(meta.agent, meta.uid)
        meta.match_turn = 0
        meta.match_off_branch = False
        if meta.metadata_only:
            turns, notes = [], []
            if report:
                report.metadata_only(meta.agent)
        else:
            if prefilter and not source_may_match(meta, prefilter):
                if report:
                    report.prefiltered(meta.agent)
                continue
            turns, notes = parse_session(meta, search_only=True, all_branches=prefilter is not None,
                                        prefilter=prefilter)
            if meta.status == "UNREADABLE":
                if report:
                    report.failed(meta.agent, meta.uid, "source unreadable or invalid active branch")
                continue
            if report:
                report.readable(meta.agent, meta.fidelity)
                if meta.parse_warnings:
                    report.warning(meta.agent, "some source records or branches could not be decoded")
            if not turns and not notes:
                continue
        if project and project not in meta.cwd.lower():
            continue
        if model and not any(model in name.lower() for name in meta.models):
            continue
        if not q or by_id:
            meta.match_evidence = "session identifier matched" if by_id else "recent activity; no query supplied"
            results.append((0, meta, turns, notes))
            if len(results) >= limit:
                break
            continue
        match = match_evidence(meta, turns, notes, q_low)
        if match:
            rank = record_match(meta, match)
            results.append((rank, meta, turns, notes))

    if q and not by_id:
        results = rank_reposted_matches(results, q_low)
    results.sort(key=lambda x: (x[0], x[1].mtime), reverse=True)
    return [(m, t, n) for _, m, t, n in results[:limit]]


PICKUP_REQUEST = re.compile(r"^\s*(?:[$/]pickup\b|\[\$pickup\]\(|<command-message>\s*pickup\s*</command-message>)", re.IGNORECASE)
QUESTION_REPLY = "<send_user_message_question_reply>"


def search_fields(meta, turns, notes, keyword):
    yield 6, None, "title", meta.title
    if meta.cwd:
        yield 5, None, "workspace name", pathlib.Path(meta.cwd).name
    yield 2, None, "workspace", meta.cwd
    yield 2, None, "identity", f"{meta.uid} {meta.raw_id} {meta.agent}"
    for turn in turns:
        pickup_request = bool(PICKUP_REQUEST.match(turn.user_text))
        question_reply = turn.user_text.startswith(QUESTION_REPLY)
        user_label = "pickup request" if pickup_request else ("answer to tool question" if question_reply else "user")
        if not turn.inherited_prompt:
            yield (3 if pickup_request or question_reply else 4), turn, user_label, turn.user_text
        # A repeated lookup clue or a tool-guided follow-up is secondary evidence.
        follow_up = question_reply or (pickup_request and keyword in turn.user_text.lower())
        assistant_label = "assistant follow-up" if follow_up else "assistant"
        for label, texts, rank in ((assistant_label, turn.assistant_texts, 3 if follow_up else 4),
                                   ("tool summary", turn.tool_summaries, 2),
                                   ("tool output", turn.tool_outputs, 1), ("error", turn.errors, 1)):
            for text in texts:
                yield rank, turn, label, text
    for note in notes:
        yield 1, None, "session note", note


def record_match(meta, match):
    rank, meta.match_evidence, turn = match
    meta.match_turn = turn.index if turn else 0
    meta.match_off_branch = bool(turn and turn.off_branch)
    return rank


def match_evidence(meta, turns, notes, keyword, reposts=None):
    remaining = dict.fromkeys(keyword.split())
    best = None
    labels = []
    for rank, turn, label, text in search_fields(meta, turns, notes, keyword):
        if reposts and id(turn) in reposts and rank == 4:
            rank = 3
            source, citation = reposts[id(turn)]
            reason = "citing an earlier match" if citation else f"in probable repost (earlier match: {source})"
            label = f"{label} {reason}" + (f" ({source})" if citation else "")
        if best and rank <= best[0]:
            continue
        lower = text.lower()
        location = f"{label}, Turn {turn.index}" if turn else label
        if turn and turn.off_branch:
            location += " (off-branch)"
        if keyword in lower:
            best = (rank, f"{location}: {one_line(match_excerpt(text, keyword, 65), 200)}", turn)
            continue
        if best:
            continue
        for token in list(remaining):
            if token in lower:
                labels.append(f"{token}: {location}")
                del remaining[token]
    if best:
        return best
    if not remaining:
        return 0, "words matched across fields: " + one_line("; ".join(labels), 200), None
    return None


def passage_windows(lower, compact_keyword, width):
    compact = re.sub(r"\s+", "", lower)
    if len(compact) < width:
        return
    offset = compact.find(compact_keyword)
    while offset >= 0:
        end = offset + len(compact_keyword)
        for start in range(max(0, end - width), min(offset, len(compact) - width) + 1):
            yield compact[start:start + width]
        offset = compact.find(compact_keyword, offset + len(compact_keyword))


def rank_reposted_matches(results, keyword):
    if len(results) < 2 or not any(rank == 4 and meta.match_evidence.startswith("user,")
                                  for rank, meta, _, _ in results):
        return results
    compact_keyword = re.sub(r"\s+", "", keyword)
    # Require a substantial shared passage and context besides the search clue.
    width = max(64, len(compact_keyword) + 24)
    origins = {}
    cited_sources = {}
    for _, meta, turns, notes in results:
        for rank, turn, label, text in search_fields(meta, turns, notes, keyword):
            if rank != 4 or label != "assistant" or not turn.timestamp:
                continue
            lower = text.lower()
            if keyword not in lower:
                continue
            origin = (turn.timestamp, meta.agent, meta.raw_id, meta.uid)
            first = cited_sources.setdefault(meta.uid, origin)
            if origin < first:
                cited_sources[meta.uid] = origin
            for window in passage_windows(lower, compact_keyword, width):
                previous = origins.setdefault(window, origin)
                if origin < previous:
                    origins[window] = origin
    if not cited_sources:
        return results

    ranked = []
    for rank, meta, turns, notes in results:
        if rank == 4 and meta.match_evidence.startswith("user,"):
            reposts = {}
            for turn in turns:
                if not turn.timestamp or turn.inherited_prompt or PICKUP_REQUEST.match(turn.user_text):
                    continue
                lower = turn.user_text.lower()
                if keyword not in lower:
                    continue
                # A lookup may cite the source ID alongside a short clue, with
                # too little shared prose to qualify as a verbatim repost.
                for timestamp, agent, raw_id, uid in cited_sources.values():
                    if (timestamp < turn.timestamp and (agent, raw_id) != (meta.agent, meta.raw_id)
                            and any(reference in lower for reference in (uid.lower(), raw_id.lower()))):
                        reposts[id(turn)] = (uid, True)
                        break
                if id(turn) in reposts:
                    continue
                for window in passage_windows(lower, compact_keyword, width):
                    origin = origins.get(window)
                    if origin and origin[0] < turn.timestamp and origin[1:3] != (meta.agent, meta.raw_id):
                        reposts[id(turn)] = (origin[3], False)
                        break
            if reposts:
                rank = record_match(meta, match_evidence(meta, turns, notes, keyword, reposts=reposts))
        ranked.append((rank, meta, turns, notes))
    return ranked


def render_candidates_table(items: list[tuple[SessionMeta, list[Turn], list[str]]], query: str | None) -> str:
    lines = ["## Recent / Matching Sessions (Global across all agents)"]
    for idx, (m, turns, _) in enumerate(items, 1):
        label = m.title or (pathlib.Path(m.cwd).name if m.cwd else "(no-cwd)")
        latest = next((turn for turn in reversed(turns) if not turn.off_branch), None)
        last_u = one_line(latest.user_text, 70) if latest else ""
        last_prompt = f"   last prompt: {last_u}\n" if last_u else ""
        source = f" | source={m.location}" if m.location else ""
        kind = " | subagent" if m.is_subagent else ""
        models = f" | models={', '.join(sorted(m.models))}" if m.models else ""
        record = f"\n   record: {m.path.name}" if m.record_variant else ""
        details = ("metadata only | transcript unavailable" if m.metadata_only else
                   f"turns={sum(not turn.inherited_prompt for turn in turns)}")
        if m.fidelity:
            details += f" | {SOURCE_FIDELITY[m.fidelity]}"
        open_match = ""
        if m.match_turn:
            tool_hit = m.match_evidence.startswith(("tool output,", "tool summary,", "error,"))
            selector = ["--turns", str(m.match_turn)]
            if tool_hit and query:
                selector = ["--grep=" + query] if query.startswith("-") else ["--grep", query]
            if m.match_off_branch and not tool_hit:
                selector.append("--all-branches")
            command = shlex.join(["pickup", "read", m.uid, *selector])
            open_match = f"\n   open match: {command}"
        updated = fmt_ts(m.mtime) if m.mtime else "unknown date"
        lines.append(
            f"{idx}. `{m.uid}` | {m.agent}{kind} | {updated} | {details} | {label} | cwd=`{m.cwd or '-'}`{source}{models}\n"
            f"{last_prompt}   match: {m.match_evidence}{open_match}{record}"
        )
    return "\n".join(lines)


def render_session_overview(meta: SessionMeta, turns: list[Turn], notes: list[str], tail_count: int = 4,
                            *, all_branches=False) -> str:
    out: list[str] = []
    out.append(f"# Picked Session: `{meta.uid}` ({meta.agent})")
    out.append(f"- full_id: `{meta.raw_id}`")
    if meta.is_subagent:
        out.append("- kind: subagent session")
    updated = fmt_ts(meta.mtime) if meta.mtime else "unknown date"
    out.append(f"- updated: {updated}")
    if meta.title:
        out.append(f"- title: {meta.title}")
    if meta.cwd:
        out.append(f"- workspace: `{meta.cwd}`")
    if meta.status:
        out.append(f"- status: `{meta.status}`")
    if meta.metadata_only:
        out.append("- view_scope: metadata only; the local transcript is unavailable")
        return "\n".join(out)
    if meta.fidelity:
        out.append(f"- fidelity: {SOURCE_FIDELITY[meta.fidelity]}")
    else:
        out.append("- source_file below points to raw storage. Use export for a readable Markdown file.")
    out.append(f"- source_file: `{meta.path}`")
    out.append(f"- total_turns: {len(turns)}")
    if meta.agent == "qoder":
        scope = "all branches" if all_branches else "active branch when links are available"
        out.append(f"- branch_scope: {scope}")
    out.append("- view_scope: shortened overview, not full dialogue or raw logs")

    # 2. Background / Task notes (if any)
    if notes:
        out.append("\n## Session Notes and Background Task Logs")
        for n in notes:
            out.append(f"- {n}")

    # 3. Detailed Tail Turns
    start_idx = max(0, len(turns) - tail_count)
    tail_turns = turns[start_idx:]
    if tail_turns:
        out.append(f"\n## Latest Turns Detail (Turns {tail_turns[0].index}..{tail_turns[-1].index})")
    else:
        out.append("\nNo dialogue turns are stored; available context is shown in session notes.")
    for t in tail_turns:
        out.append("\n" + turn_heading(t))
        out.append(f"**User**:\n{truncate_block(t.user_text, 1500)}")
        if t.tool_summaries:
            shown = t.tool_summaries[-12:]
            prefix = f"(+{len(t.tool_summaries) - 12} earlier tools) " if len(t.tool_summaries) > 12 else ""
            out.append(f"**Tools ({len(t.tool_summaries)})**: {prefix}" + " -> ".join(f"`{s}`" for s in shown))
        if t.assistant_texts:
            joined_a = "\n\n".join(t.assistant_texts)
            out.append(f"**Assistant**:\n{truncate_block(joined_a, 2400)}")
        if t.errors:
            out.append("**Errors**:\n" + "\n".join(f"- {e}" for e in t.errors[-5:]))

    # 1. Full User Prompts Outline (every user prompt across the entire conversation!)
    bounds = f" ({turns[0].index}..{turns[-1].index})" if turns else ""
    out.append(f"\n## All User Prompts Outline{bounds}")
    for t in turns:
        err_tag = " [ERR]" if t.errors else ""
        off_branch = " [off-branch]" if t.off_branch else ""
        out.append(f"- [Turn {t.index}] ({t.timestamp}){off_branch}{err_tag}: {one_line(t.user_text, 160)}")


    prefix = shlex.join([sys.executable, str(pathlib.Path(__file__).resolve())])
    uid = shlex.quote(meta.uid)
    branch_flag = " --all-branches" if all_branches else ""
    first, last = (turns[0].index, turns[min(14, len(turns) - 1)].index) if turns else (1, 1)
    sample = f"{first}-{last}" if first < last else str(first)
    out.append(
        f"\n---\nNeed earlier or full dialogue? Run:\n"
        f"- Full read-through: `{prefix} read {uid} --all{branch_flag}`\n"
        f"- Specific turns:    `{prefix} read {uid} --turns {sample}{branch_flag}`\n"
        f"- Search in session: `{prefix} read {uid} --grep \"<keyword>\"`"
    )
    return "\n".join(out)


def turn_bounds(turn_range):
    match = re.fullmatch(r"([1-9]\d*)(?:-([1-9]\d*))?", turn_range)
    if not match:
        raise ValueError("--turns must be a positive number or range")
    start = int(match[1])
    end = int(match[2]) if match[2] else start
    if start > end:
        raise ValueError("--turns range must be ascending")
    return start, end


def select_turns(turns, turn_range, *, require_full_range=False):
    start, end = turn_bounds(turn_range)
    selected = [turn for turn in turns if start <= turn.index <= end]
    if not selected:
        raise ValueError(f"No turns in requested range ({turn_range}); this session has {len(turns)} turns")
    last_index = max(turn.index for turn in turns)
    if require_full_range and end > last_index:
        raise ValueError(f"Requested range ({turn_range}) exceeds the last turn ({last_index})")
    return selected


def fenced_snippet(text):
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def turn_heading(turn):
    suffix = " [off-branch]" if turn.off_branch else ""
    return f"### Turn {turn.index} ({turn.timestamp}){suffix}"


def render_read_turns(
    meta: SessionMeta,
    turns: list[Turn],
    turn_range: str | None,
    read_all: bool,
    grep_kw: str | None,
    with_tools: bool,
    max_chars: int,
    notes: list[str] | None = None,
    *,
    all_branches=False,
) -> str:
    """Build a decoded view; CLI pagination enforces the output budget."""
    out: list[str] = [f"# Session `{meta.uid}` ({meta.agent}) — {meta.title or meta.cwd or meta.raw_id}"]
    if meta.fidelity:
        out.extend((f"- fidelity: {SOURCE_FIDELITY[meta.fidelity]}", f"- source_file: `{meta.path}`"))
    if meta.agent == "qoder":
        scope = "all branches" if all_branches or grep_kw else "active branch when links are available"
        out.append(f"- branch_scope: {scope}")

    if grep_kw:
        kw_low = grep_kw.lower()
        hits = 0
        out.append(f"Search keyword: `{grep_kw}`\n")
        for note in notes or []:
            if kw_low in note.lower():
                hits += 1
                out.append(f"**Session Note**: {match_excerpt(note, grep_kw)}")
        for t in turns:
            prompt = "" if t.inherited_prompt else t.user_text
            hay_parts = [prompt, *t.assistant_texts, *t.tool_summaries, *t.tool_outputs, *t.errors]
            if any(kw_low in p.lower() for p in hay_parts):
                hits += 1
                out.append(turn_heading(t))
                out.append(f"**User**: {match_excerpt(t.user_text, grep_kw) if kw_low in t.user_text.lower() else one_line(t.user_text, 300)}")
                matching_tools = [s for s in t.tool_summaries if kw_low in s.lower()]
                if matching_tools:
                    out.append("**Matching Tools**: " + ", ".join(f"`{s}`" for s in matching_tools[:10]))
                for at in t.assistant_texts:
                    if kw_low in at.lower():
                        out.append(f"**Assistant**:\n{match_excerpt(at, grep_kw)}")
                for to in t.tool_outputs:
                    if kw_low in to.lower():
                        snippet = match_excerpt(to, grep_kw)
                        out.append(f"**Tool Output Snippet**:\n{fenced_snippet(snippet)}")
                for error in t.errors:
                    if kw_low in error.lower():
                        out.append(f"**Error**: {match_excerpt(error, grep_kw)}")
        if hits == 0:
            out.append("No dialogue, tool outputs, or session notes matched that keyword.")
        return "\n".join(out)

    if notes:
        out.append("\n## Session Notes\n" + "\n".join(f"- {note}" for note in notes))

    selected = select_turns(turns, turn_range) if turn_range and not read_all else turns

    for t in selected:
        block_lines = ["\n" + turn_heading(t)]
        block_lines.append(f"**User**:\n{t.user_text}")
        if t.tool_summaries:
            block_lines.append(f"**Tools ({len(t.tool_summaries)})**: " + ", ".join(f"`{s}`" for s in t.tool_summaries[:20]))
        if with_tools and t.tool_outputs:
            outputs = t.tool_outputs if meta.agent == "qoder" else t.tool_outputs[-5:]
            for to in outputs:
                block_lines.append(f"**Tool Output**:\n{fenced_snippet(truncate_block(to, 1200))}")
        if t.assistant_texts:
            block_lines.append("**Assistant**:\n" + "\n\n".join(t.assistant_texts))
        if t.errors:
            block_lines.append("**Errors**: " + "; ".join(t.errors[-3:]))

        block = "\n".join(block_lines)
        out.append(block)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def session_fingerprint(meta, turns, notes, content=""):
    value = {"session_id": f"{meta.agent}:{meta.raw_id}", "title": meta.title,
             "cwd": meta.cwd, "path": str(meta.path), "status": meta.status, "mtime": meta.mtime,
             "turns": [asdict(turn) for turn in turns], "notes": notes,
             "renderer_version": 1, "parse_warnings": meta.parse_warnings,
             "rendered_view_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def load_selected_meta(target):
    target = normalize_session_target(target)
    source = target.partition(":")[0].lower()
    metas = discover_all([source] if source in AGENTS else None, include_subagents=True)
    meta = resolve_by_id(metas, target)
    if meta is None:
        raise ValueError("Session not found; check the selected source or full ID")
    return meta


def load_decoded(meta, *, all_branches=False):
    if meta.metadata_only:
        raise ValueError("Only session metadata is available; the local transcript is missing")
    turns, notes = parse_session(meta, use_full=True, all_branches=all_branches)
    if meta.status == "UNREADABLE":
        raise ValueError("Session could not be read; resolve its source error before retrying")
    if not turns and not notes:
        raise ValueError("Session has no readable decoded content")
    return turns, notes


def export_session(meta, destination, output_format, with_tools, turn_ranges, *, all_branches=False):
    if meta.metadata_only:
        raise ValueError("Only session metadata is available; the local transcript is missing")
    if output_format == "raw" and meta.fidelity:
        raise ValueError(f"{SOURCE_FIDELITY[meta.fidelity]} lacks raw events. Use the Markdown export.")
    if output_format == "markdown":
        turns, notes = load_decoded(meta, all_branches=all_branches)
        scope = "Decoded dialogue and tool summaries"
        if turn_ranges:
            selections = [select_turns(turns, period, require_full_range=True) for period in turn_ranges]
            indices = {turn.index for selection in selections for turn in selection}
            total = len(turns)
            turns = [turn for turn in turns if turn.index in indices]
            scope += f" for {len(turns)} selected turns of {total}"
            notes = []
        if with_tools:
            scope += " with tool output snippets"
        content = render_read_turns(meta, turns, None, True, None, with_tools, 0,
                                    notes=notes, all_branches=all_branches)
        heading = f"> {scope}. " + (f"{SOURCE_FIDELITY[meta.fidelity]}." if meta.fidelity else
                                    "Raw events are available through raw export.")
        if meta.parse_warnings:
            heading += "\n> Source decoding is partial."

    # Publish only after the file is fully written. link() also protects an
    # existing destination if two exports race or it is a dangling symlink.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".pickup-", delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            if output_format == "markdown":
                stream.write(f"{heading}\n\n{content}\n".encode("utf-8"))
            elif meta.agent == "devin":
                pickup_extra.export_devin_records(meta.path, stream)
            else:
                source = (meta.path / "agents" / "main" / "wire.jsonl") if meta.agent == "kimi" else meta.path
                with source.open("rb") as original:
                    before = os.fstat(original.fileno())
                    shutil.copyfileobj(original, stream)
                    after = os.fstat(original.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("Source changed during export; retry when the session is stable")
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_cli(argv):
    parser = argparse.ArgumentParser(prog="pickup export", description="Save a decoded Markdown transcript or raw source records to a new local file.")
    parser.add_argument("session_id", help="Session UID, full ID or session link")
    parser.add_argument("--output", "-o", type=pathlib.Path, required=True, help="Path to a new file")
    parser.add_argument("--format", choices=("markdown", "raw"), default="markdown",
                        help="Markdown dialogue or raw source bytes (Devin: session-scoped JSONL)")
    parser.add_argument("--with-tools", action="store_true", help="Include tool output snippets in Markdown")
    parser.add_argument("--turns", action="append", default=[], help="Export these turns as Markdown, e.g. 8-20; repeatable")
    parser.add_argument("--all-branches", action="store_true", help="Include Qoder's off-branch turns in Markdown")
    args = parser.parse_args(argv)
    if args.format == "raw" and (args.with_tools or args.turns or args.all_branches):
        parser.error("--with-tools, --turns, and --all-branches apply only to Markdown")
    destination = args.output.expanduser()
    try:
        if os.path.lexists(destination):
            raise ValueError("Output already exists; choose a new file")
        meta = load_selected_meta(args.session_id)
        export_session(meta, destination, args.format, args.with_tools, args.turns, all_branches=args.all_branches)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    print(f"Saved {args.format} export: {destination.absolute()}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "export":
        return export_cli(argv[1:])
    if argv and argv[0] in ("read", "show"):
        operation = argv[0]
        parser = argparse.ArgumentParser(prog=f"pickup {operation}", description="Read a bounded decoded view. Follow its cursor until complete=yes; this does not export raw logs.")
        parser.add_argument("session_id", help="Session UID, full ID, or session link")
        parser.add_argument("--max-chars", type=int, help="Entire page character budget (minimum: 1024); alone allows up to four UTF-8 bytes per character")
        parser.add_argument("--max-bytes", type=int, help="Entire UTF-8 page budget (default with neither limit: 8000)")
        parser.add_argument("--all-branches", action="store_true", default=None, help="Include Qoder's off-branch turns")
        parser.add_argument("--cursor", help="Resume a page; restores its original view options. Changed sessions require a restart.")
        if operation == "show":
            parser.add_argument("--tail", type=int, help="Recent turns in the shortened overview (default: 4)")
        else:
            group = parser.add_mutually_exclusive_group()
            group.add_argument("--all", action="store_true", default=None, help="Read decoded dialogue from the beginning, paginated; not raw-log export")
            group.add_argument("--turns", help="Positive turn number or inclusive range, e.g. 8-20")
            group.add_argument("--grep", help="Search dialogue, tool excerpts and notes, including Qoder's off-branch turns")
            parser.add_argument("--with-tools", action="store_true", default=None, help="Include retained tool-output snippets")
        args = parser.parse_args(argv[1:])
        try:
            if args.max_chars is not None and args.max_chars < pickup_paging.MIN_MAX_CHARS:
                raise ValueError(f"--max-chars must be at least {pickup_paging.MIN_MAX_CHARS}")
            if args.max_bytes is not None and args.max_bytes < 1:
                raise ValueError("--max-bytes must be positive")
            supplied = {key: value for key, value in vars(args).items()
                        if key not in ("session_id", "max_chars", "max_bytes", "cursor") and value is not None}
            if args.cursor:
                state = pickup_paging.decode_cursor(args.cursor)
                if state["operation"] != operation:
                    raise ValueError("Cursor operation differs; restart the selected view")
                if supplied:
                    raise ValueError("A cursor restores view options; pass only --cursor and optional page limits")
                options = state["options"]
            elif operation == "show":
                options = {"tail": args.tail if args.tail is not None else 4}
            else:
                options = {"all": bool(args.all), "turns": args.turns, "grep": args.grep,
                           "with_tools": bool(args.with_tools)}
            if args.all_branches:
                options["all_branches"] = True
            all_branches = options.get("all_branches", False)
            if type(all_branches) is not bool:
                raise ValueError("Invalid branch view option")
            keys = options.keys() - {"all_branches"}
            if operation == "show":
                if keys != {"tail"} or type(options["tail"]) is not int or options["tail"] < 1:
                    raise ValueError("Invalid overview options; --tail must be positive")
            else:
                if keys != {"all", "turns", "grep", "with_tools"} or any(type(options[k]) is not bool for k in ("all", "with_tools")):
                    raise ValueError("Invalid dialogue view options")
                for key in ("turns", "grep"):
                    if options[key] is not None and not isinstance(options[key], str):
                        raise ValueError("Invalid dialogue view options")
                if sum(bool(options[k]) for k in ("all", "turns", "grep")) > 1:
                    raise ValueError("Choose only one of --all, --turns, or --grep")
                turn_range = options["turns"]
                if turn_range is not None:
                    turn_bounds(turn_range)
                if options["grep"] is not None and not options["grep"].strip():
                    raise ValueError("--grep must not be empty")
            meta = load_selected_meta(args.session_id)
            turns, notes = (([], []) if operation == "show" and meta.metadata_only else
                            load_decoded(meta, all_branches=all_branches or bool(options.get("grep"))))
            if operation == "show":
                content = render_session_overview(meta, turns, notes, tail_count=options["tail"],
                                                  all_branches=all_branches)
            else:
                content = render_read_turns(meta, turns, options["turns"], not options["turns"] and not options["grep"],
                                            options["grep"], options["with_tools"], args.max_chars,
                                            notes=notes, all_branches=all_branches)
            if meta.parse_warnings:
                content = "Source decoding: partial; some records or branches were not decoded.\n\n" + content
            selection = meta.uid if meta.record_variant else f"{meta.agent}:{meta.raw_id}"
            page = pickup_paging.render_page(content, selection,
                        session_fingerprint(meta, turns, notes, content), operation, options,
                        args.max_chars, args.cursor, command_prefix=[sys.executable, str(pathlib.Path(__file__).resolve())],
                        max_bytes=args.max_bytes)
        except ValueError as error:
            parser.error(str(error))
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write((page + "\n").encode("utf-8"))
        else:
            print(page)
        return 0

    parser = argparse.ArgumentParser(prog="pickup", description="Find local agent sessions across directories. Use show/read for bounded pages or export to save a local file.")
    parser.add_argument("query", nargs="*", help="Optional quote, title, project name, or keyword")
    parser.add_argument("--list", action="store_true", help="List candidates (the default; compatibility flag)")
    parser.add_argument("--agent", choices=AGENTS, help="Discover and search only this source")
    parser.add_argument("--project", metavar="PATH_TEXT", help="Only search workspaces whose paths contain this text")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="Only search records modified on or after this local date")
    parser.add_argument("--model", metavar="NAME_TEXT", help="Match model names recorded in Codex, Antigravity, Claude Code, or Qoder transcripts")
    parser.add_argument("--include-subagents", action="store_true", help="Include local Antigravity, Codex, Claude Code, and Qoder subagents")
    parser.add_argument("--exclude", action="append", default=[], metavar="ID", help="Exclude an exact full ID or UID; repeatable")
    parser.add_argument("--limit", type=int, default=6, help="Candidate count (default: 6)")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    model = (args.model or "").strip()
    try:
        since = dt.datetime.combine(dt.date.fromisoformat(args.since), dt.time.min).timestamp() if args.since else None
    except (ValueError, OSError, OverflowError):
        parser.error("--since must be a local date in YYYY-MM-DD format")
    query = normalize_session_target(" ".join(args.query)) or None
    prefix, _, fragment = (query or "").partition(":")
    explicit_source = prefix.lower() if prefix.lower() in AGENTS and fragment and not re.search(r"\s", fragment) else None
    by_id = bool(explicit_source or FULL_SESSION_ID.fullmatch(query or ""))
    selected = args.agent or explicit_source
    model_sources = ("antigravity", "codex", "claude", "qoder")
    if selected and model and selected not in model_sources:
        parser.error("--model is available for Antigravity, Codex, Claude Code, and Qoder")
    sources = (selected,) if selected else (model_sources if model else AGENTS)
    report = ScanReport(sources)
    metas = discover_all(sources, report=report, include_subagents=args.include_subagents or by_id,
                         batch_indexed_text=bool(query and not by_id))
    matches = find_matching_sessions(metas, query, args.limit, set(args.exclude), report=report, by_id=by_id,
                                     project=(args.project or "").strip(), since=since, model=model)
    status = report.coverage_label(report.finish())
    if not matches:
        print(f"Scan coverage: {status}.")
        if not metas:
            print("No local sessions discovered. Check the source paths and diagnostics above; absent stores differ from failed discovery.")
        else:
            print("No matching sessions found. Check the query, filters, and scan coverage.")
        return 1
    print(render_candidates_table(matches, query))
    if by_id:
        print("\nLookup checked the requested identifier. Unrelated session content was skipped.")
    else:
        print(f"\nScan coverage: {status}. Use the role and excerpt to identify the original session.")
    print("Open a candidate with: pickup show <uid>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
