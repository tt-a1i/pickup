"""Readers for locally stored Kimi Code and Devin CLI conversations."""
import datetime as dt
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote


def _time(value):
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000 if value > 100000000000 else value).strftime('%Y-%m-%d %H:%M')
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError, OSError, OverflowError):
        return ''


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str)).strip()
    return ''


def _turn(text, timestamp):
    return dict(user_text=text, timestamp=_time(timestamp), assistant_texts=[], tool_summaries=[], tool_outputs=[], errors=[])


def _tool(call):
    function = call.get('function') if isinstance(call.get('function'), dict) else call
    name = function.get('name') or 'tool'
    args = function.get('arguments') or function.get('args') or ''
    if isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False)
    args = ' '.join(str(args).split())
    return f'{name}({args[:90]})' if args else str(name)


def discover_kimi(root=None):
    root = Path(root) if root is not None else Path.home() / '.kimi-code' / 'sessions'
    found = []
    for state in root.glob('wd_*/session_*/state.json'):
        try:
            obj = json.loads(state.read_text())
            wire = state.parent / 'agents' / 'main' / 'wire.jsonl'
            if not wire.is_file():
                continue
            found.append(dict(raw_id=str(obj.get('id') or state.parent.name.removeprefix('session_')), path=str(state.parent), mtime=wire.stat().st_mtime, cwd=obj.get('cwd') or '', title=obj.get('title') or ''))
        except (OSError, ValueError, TypeError):
            continue
    return found


def parse_kimi(path):
    wire = Path(path) / 'agents' / 'main' / 'wire.jsonl'
    turns = []
    with wire.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if event.get('type') != 'agent.message.appended':
                continue
            envelope = event.get('message') or {}
            message = envelope.get('message') or {}
            role = message.get('role')
            text = _text(message.get('content'))
            if role == 'user':
                if text:
                    turns.append(_turn(text, (envelope.get('meta') or {}).get('createdAt') or event.get('time')))
            elif turns and role == 'assistant':
                if text:
                    turns[-1]['assistant_texts'].append(text)
                turns[-1]['tool_summaries'].extend(_tool(c) for c in message.get('toolCalls', []) if isinstance(c, dict))
            elif turns and role == 'tool' and text:
                turns[-1]['tool_outputs'].append(text)
    return turns


def _db(path):
    return sqlite3.connect('file:' + quote(str(Path(path).resolve())) + '?mode=ro', uri=True)


def discover_devin(db=None):
    db = Path(db) if db is not None else Path.home() / '.local' / 'share' / 'devin' / 'cli' / 'sessions.db'
    if not db.is_file():
        return []
    try:
        with _db(db) as conn:
            rows = conn.execute('SELECT id, working_directory, title, last_activity_at FROM sessions WHERE hidden = 0').fetchall()
        return [dict(raw_id=str(sid), path=f'{db}#{sid}', mtime=float(ts or 0), cwd=cwd or '', title=title or '') for sid, cwd, title, ts in rows]
    except (sqlite3.Error, OSError, ValueError):
        return []


def parse_devin(path):
    db, sep, sid = str(path).rpartition('#')
    if not sep or not db or not sid:
        raise ValueError('Devin session path must include database path and session ID')
    with _db(db) as conn:
        row = conn.execute('SELECT main_chain_id FROM sessions WHERE id = ?', (sid,)).fetchone()
        if row is None:
            raise ValueError('Devin session not found')
        # Only ancestors of the active head belong to the active conversation.
        rows = conn.execute('SELECT node_id, parent_node_id, chat_message, created_at FROM message_nodes WHERE session_id = ?', (sid,)).fetchall()
    nodes = {node: (parent, raw, created) for node, parent, raw, created in rows}
    chain, seen, current = [], set(), row[0]
    while current is not None and current in nodes and current not in seen:
        seen.add(current)
        parent, raw, created = nodes[current]
        chain.append((raw, created))
        current = parent
    turns = []
    for raw, created in reversed(chain):
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            continue
        role, text = message.get('role'), _text(message.get('content'))
        meta = message.get('metadata') or {}
        if role == 'user' and meta.get('is_user_input') is True and text:
            turns.append(_turn(text, meta.get('created_at') or created))
        elif turns and role == 'assistant':
            if text:
                turns[-1]['assistant_texts'].append(text)
            turns[-1]['tool_summaries'].extend(_tool(c) for c in (message.get('tool_calls') or []) if isinstance(c, dict))
        elif turns and role == 'tool' and text:
            turns[-1]['tool_outputs'].append(text)
            result = (meta.get('extensions') or {}).get('chisel/tool_result_meta') or {}
            if result.get('success') is False:
                turns[-1]['errors'].append(text)
    return turns
