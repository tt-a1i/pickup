"""SQLite session stores, Kimi Code wire logs, and Qoder branch links."""
import datetime as dt
import os
from contextlib import closing
import json
import sqlite3
from pathlib import Path


def _time(value):
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000 if value > 100000000000 else value).strftime('%Y-%m-%d %H:%M')
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError, AttributeError, OSError, OverflowError):
        return ''


def _text(content, *, image_attachments=False, clean_text=None):
    if isinstance(content, str):
        return clean_text(content) if clean_text else content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind == 'text' and isinstance(part.get('text'), str):
                text = clean_text(part['text']) if clean_text else part['text']
                if text or not clean_text:
                    parts.append(text)
            elif image_attachments and kind in ('localImage', 'image'):
                path = part.get('path')
                parts.append(f'[Image attachment: {path}]' if isinstance(path, str) and path else '[Image attachment]')
        return '\n'.join(parts).strip()
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


def discover_kimi(root=None, on_error=None, *, include_subagents=False):
    root = Path(root) if root is not None else Path.home() / '.kimi-code' / 'sessions'
    found = []

    def report(reason):
        if on_error:
            on_error('kimi', reason)

    def directories(path, prefix):
        try:
            with os.scandir(path) as entries:
                return [Path(entry.path) for entry in entries
                        if entry.name.startswith(prefix) and entry.is_dir()]
        except FileNotFoundError:
            return []
        except OSError:
            report('Cannot read session directory')
            return []

    for workdir in directories(root, 'wd_'):
        for session in directories(workdir, 'session_'):
            state = session / 'state.json'
            try:
                obj = json.loads(state.read_text(encoding='utf-8'))
                if not isinstance(obj, dict):
                    raise ValueError('state must be an object')
            except (ValueError, UnicodeError):
                report('Malformed session metadata')
                continue
            except OSError:
                report('Cannot read session metadata')
                continue
            parent_id = str(obj.get('id') or session.name.removeprefix('session_'))
            agents = session / 'agents'
            agent_dirs = [agents / 'main']
            if include_subagents:
                agent_dirs.extend(path for path in directories(agents, '') if path.name != 'main')
            for agent in agent_dirs:
                wire = agent / 'wire.jsonl'
                try:
                    if not wire.is_file():
                        continue
                    subagent = agent.name != 'main'
                    # Agent IDs such as agent-0 are local to their parent session.
                    found.append(dict(raw_id=f'{parent_id}/{agent.name}' if subagent else parent_id,
                                      path=wire, mtime=wire.stat().st_mtime, cwd=obj.get('cwd') or '',
                                      title=obj.get('title') or '', is_subagent=subagent))
                except OSError:
                    report('Cannot inspect session transcript')
    return found


def parse_kimi(path, on_error=None):
    wire = Path(path)
    if wire.is_dir():
        wire = wire / 'agents' / 'main' / 'wire.jsonl'
    turns = []
    with wire.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('record must be an object')
            except (ValueError, TypeError):
                if on_error:
                    on_error('kimi', 'Malformed transcript record')
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


def open_readonly_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)


_INDEXED_CHUNK_COLUMNS = 'chunk_first_step, user_step_byte_offsets, agent_step_byte_offsets, user_body, agent_body'
_INDEXED_CHUNK_ORDER = 'ORDER BY chunk_index, rowid'


def antigravity_indexed_sessions(path, session_ids, *, batch_chunks=False):
    """Find indexed fallbacks, optionally retaining their chunks in the same FTS scan."""
    with closing(open_readonly_db(path)) as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'conversations_fts'").fetchone()
        if not table:
            return {}
        has_body = "WHERE user_body <> '' OR agent_body <> ''"
        # conversation_id is UNINDEXED. Read the table once for a corpus search.
        columns = _INDEXED_CHUNK_COLUMNS if batch_chunks else 'user_body, agent_body'
        order = _INDEXED_CHUNK_ORDER if batch_chunks else ''
        rows = conn.execute(f'SELECT conversation_id, {columns} FROM conversations_fts {has_body} {order}')
        chunks = {}
        for row in rows:
            sid = row[0]
            if sid not in session_ids or not any(body and body.strip() for body in row[-2:]):
                continue
            if batch_chunks:
                chunks.setdefault(sid, []).append(row[1:])
            else:
                chunks[sid] = None
        return chunks


def _indexed_steps(body, offsets, first_step):
    if not body or not body.strip():
        return []
    raw = body.encode('utf-8')
    try:
        starts = json.loads(offsets)
        if (not isinstance(starts, list) or not starts
                or any(not isinstance(pair, list) or len(pair) != 2
                       or any(type(value) is not int for value in pair) for pair in starts)
                or starts[0][1] != 0):
            raise ValueError('invalid step offsets')
        ends = starts[1:] + [(None, len(raw))]
        steps = []
        for (step, start), (_, end) in zip(starts, ends):
            if not 0 <= start <= end <= len(raw):
                raise ValueError('invalid step offset')
            text = raw[start:end].decode('utf-8').strip()
            if text:
                steps.append((step, text))
        return steps
    except (ValueError, TypeError):
        # The index can retain usable text even when step boundaries are absent.
        return [(first_step, body.strip())]


def antigravity_indexed_messages(path, session_id, *, chunks=None):
    if chunks is None:
        with closing(open_readonly_db(path)) as conn:
            chunks = conn.execute(
                f'SELECT {_INDEXED_CHUNK_COLUMNS} FROM conversations_fts '
                f'WHERE conversation_id = ? {_INDEXED_CHUNK_ORDER}', (session_id,),
            ).fetchall()
    messages = {}
    for first, user_offsets, agent_offsets, user_body, agent_body in chunks:
        for role, body, offsets in (('user', user_body, user_offsets), ('assistant', agent_body, agent_offsets)):
            for step, text in _indexed_steps(body, offsets, first):
                messages.setdefault((step, role == 'assistant', role, text), None)
    return [(role, text, None) for _, _, role, text in sorted(messages, key=lambda entry: entry[:2])]


def discover_codex_history(path, state_path, file_ids, *, on_error=None):
    catalog, children = {}, set()
    if state_path is not None:
        try:
            with closing(open_readonly_db(state_path)) as conn:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(threads)')}
                fields = ('id', 'rollout_path', 'cwd', 'title', 'source', 'updated_at', 'model', 'agent_role')
                select = ', '.join(field if field in columns else 'NULL' for field in fields)
                catalog = {row[0]: row[1:] for row in conn.execute(f'SELECT {select} FROM threads')}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_spawn_edges'").fetchone():
                    children = {row[0] for row in conn.execute('SELECT child_thread_id FROM thread_spawn_edges')}
        except (OSError, sqlite3.Error):
            if on_error:
                on_error('codex', 'SQLite thread metadata unavailable; reading history without it')
    stamps = {}
    try:
        if path.is_file():
            with closing(open_readonly_db(path)) as conn:
                ids = {row[0] for row in conn.execute('SELECT DISTINCT thread_id FROM thread_items')}
                for sid in ids - file_ids:
                    stamp = conn.execute(
                        "SELECT max(created_at_ms) FROM thread_items WHERE thread_id = ? "
                        "AND item_type IN ('userMessage', 'agentMessage')", (sid,),
                    ).fetchone()[0]
                    if stamp is not None:
                        stamps[sid] = stamp
    except (OSError, sqlite3.Error):
        if on_error:
            on_error('codex', 'SQLite history unavailable; falling back to rollout files')
    sessions = []
    for sid in sorted((stamps.keys() | catalog.keys()) - file_ids):
        rollout, cwd, title, source, updated, model, agent_role = catalog.get(sid, (None,) * 7)
        stamp = stamps.get(sid)
        if not rollout and stamp is None:
            continue
        try:
            source_data = json.loads(source) if source else None
        except (ValueError, TypeError):
            source_data = None
        sessions.append(dict(raw_id=sid, path=path if stamp is not None else None,
                             rollout_path=Path(rollout) if rollout else None,
                             mtime=updated or (stamp or 0) / 1000,
                             cwd=cwd or '', title=title or '', stored_model=model or '',
                             is_subagent=bool(sid in children or agent_role
                                              or isinstance(source_data, dict) and 'subagent' in source_data)))
    return sessions


def codex_history_messages(path, session_id, on_error, *, clean_user_text):
    with closing(open_readonly_db(path)) as conn:
        rows = conn.execute(
            "SELECT item_type, item_json, created_at_ms FROM thread_items WHERE thread_id = ? "
            "AND item_type IN ('userMessage', 'agentMessage') ORDER BY rollout_ordinal, item_id", (session_id,),
        )
        for kind, raw, stamp in rows:
            try:
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError('expected a message object')
                if kind == 'userMessage':
                    yield 'user', _text(item.get('content'), image_attachments=True, clean_text=clean_user_text), stamp
                else:
                    yield 'assistant', _text(item.get('text')), stamp
            except (ValueError, TypeError):
                on_error()


def _message_blocks(record):
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def qoder_branches(records, *, all_branches):
    """Partition the graph into the active path and historical paths at their forks."""
    nodes = {record['uuid']: record for record in records
             if isinstance(record.get('uuid'), str) and record['uuid']}
    if not nodes or not any('parentUuid' in record or 'logicalParentUuid' in record
                            or record.get('type') == 'active-leaf' for record in records):
        return None
    inferred_leaf = next((uuid for uuid in reversed(nodes) if nodes[uuid].get('type') in ('user', 'assistant')), None)
    if inferred_leaf is None:
        return None
    unlinked = [record for record in records if record.get('type') in ('user', 'assistant')
                and (not isinstance(record.get('uuid'), str) or not record['uuid'])]
    leaf = next((record['leafUuid'] for record in reversed(records)
                 if record.get('type') == 'active-leaf' and 'leafUuid' in record), inferred_leaf)
    invalid_leaf = leaf is not None and (not isinstance(leaf, str) or leaf not in nodes)
    if invalid_leaf:
        leaf = inferred_leaf
    incomplete = invalid_leaf or bool(unlinked)
    claimed = set()

    def take_path(tip):
        nonlocal incomplete
        path = []
        seen = set()
        while tip in nodes and tip not in claimed and tip not in seen:
            seen.add(tip)
            node = nodes[tip]
            path.append(node)
            parent = node.get('parentUuid') or node.get('logicalParentUuid')
            tip = parent if isinstance(parent, str) else None
        incomplete |= tip is not None and (tip not in nodes or tip in seen)
        claimed.update(seen)
        return (tip if tip not in seen else None), list(reversed(path))

    parent, active = take_path(leaf if isinstance(leaf, str) else None)
    file_order = {id(rec): idx for idx, rec in enumerate(nodes.values())}
    call_positions = {block['id']: position for position, record in enumerate(active)
                      if record.get('type') == 'assistant'
                      for block in _message_blocks(record)
                      if isinstance(block, dict) and block.get('type') == 'tool_use'
                      and isinstance(block.get('id'), str) and block['id']}
    if call_positions:
        def _result_owner(record):
            if record.get('type') != 'user' or record.get('isMeta'):
                return None
            blocks = _message_blocks(record)
            if blocks and all(isinstance(block, dict) and block.get('type') == 'tool_result'
                              and isinstance(block.get('tool_use_id'), str)
                              and block['tool_use_id'] in call_positions for block in blocks):
                return max(call_positions[block['tool_use_id']] for block in blocks)
            return None

        grouped_results = {}
        kept_active = []
        active_ids = claimed.copy()
        for record in active:
            owner = _result_owner(record)
            if owner is not None:
                grouped_results.setdefault(owner, []).append((record, kept_active, len(kept_active)))
            else:
                kept_active.append(record)
        has_parallel = False
        for uuid, record in nodes.items():
            call = record.get('parentUuid')
            if uuid in claimed or not isinstance(call, str) or call not in active_ids:
                continue
            owner = _result_owner(record)
            if owner is not None:
                grouped_results.setdefault(owner, []).append((record, None, 0))
                claimed.add(uuid)
                has_parallel = True
        if has_parallel:
            # Keep all tool results for a call batch grouped under that call in recorded order.
            call_to_kept = {}
            idx_kept = 0
            for pos, record in enumerate(active):
                if _result_owner(record) is None:
                    call_to_kept[pos] = idx_kept
                    idx_kept += 1
            by_kept = {
                call_to_kept[owner]: sorted(
                    (item[0] for item in items), key=lambda rec: file_order.get(id(rec), 0)
                )
                for owner, items in grouped_results.items() if owner in call_to_kept
            }
            active = [
                entry for idx, record in enumerate(kept_active)
                for entry in (record, *by_kept.get(idx, ()))
            ]
    paths = [(parent, active)]
    if all_branches:
        for tip in reversed(nodes):
            if tip not in claimed:
                paths.append(take_path(tip))
        if unlinked:
            paths.append((None, unlinked))
    return paths, incomplete


def discover_devin(db=None, on_error=None):
    db = Path(db) if db is not None else Path.home() / '.local' / 'share' / 'devin' / 'cli' / 'sessions.db'
    try:
        db.stat()
    except FileNotFoundError:
        return []
    except OSError:
        if on_error:
            on_error('devin', 'Cannot inspect session database')
        return []
    try:
        with closing(open_readonly_db(db)) as conn:
            rows = conn.execute('SELECT id, working_directory, title, last_activity_at FROM sessions WHERE hidden = 0').fetchall()
        return [dict(raw_id=str(sid), path=f'{db}#{sid}', mtime=float(ts or 0), cwd=cwd or '', title=title or '') for sid, cwd, title, ts in rows]
    except (sqlite3.Error, OSError, ValueError):
        if on_error:
            on_error('devin', 'Cannot read session database or its schema')
        return []


def _devin_session_rows(path):
    db, sep, sid = str(path).rpartition('#')
    if not sep or not db or not sid:
        raise ValueError('Devin session path must include database path and session ID')
    with closing(open_readonly_db(db)) as conn:
        conn.execute('BEGIN')
        row = conn.execute('SELECT main_chain_id FROM sessions WHERE id = ?', (sid,)).fetchone()
        if row is None:
            raise ValueError('Devin session not found')
        rows = conn.execute(
            'SELECT node_id, parent_node_id, chat_message, created_at FROM message_nodes WHERE session_id = ?',
            (sid,),
        ).fetchall()
    return sid, row[0], rows


def export_devin_records(path, stream):
    sid, head, rows = _devin_session_rows(path)
    records = [{'type': 'session', 'session_id': sid, 'main_chain_id': head}]
    records.extend(dict(type='message_node', node_id=node, parent_node_id=parent,
                        chat_message=raw, created_at=created) for node, parent, raw, created in rows)
    for record in records:
        stream.write((json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'))


def parse_devin(path, on_error=None):
    _, head, rows = _devin_session_rows(path)
    # Only ancestors of the active head belong to the active conversation.
    nodes = {node: (parent, raw, created) for node, parent, raw, created in rows}
    chain, seen, current = [], set(), head
    while current is not None and current in nodes and current not in seen:
        seen.add(current)
        parent, raw, created = nodes[current]
        chain.append((raw, created))
        current = parent
    if on_error and ((head is None and nodes) or (current is not None and (current not in nodes or current in seen))):
        on_error('devin', 'Active message chain is missing or cyclic')
    turns = []
    for raw, created in reversed(chain):
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError('message must be an object')
        except (ValueError, TypeError):
            if on_error:
                on_error('devin', 'Malformed message record')
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
