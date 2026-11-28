"""Exercise the public CLI in isolated homes; never read personal histories."""
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import pickup


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {**os.environ, 'HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.sid = 'fixture-session-id'
        self.path = self.home / '.codex/sessions/fixture.jsonl'
        self.path.parent.mkdir(parents=True)

    def message(self, role, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}

    def write(self, rows, *, sid=None, path=None, cwd='/fixture'):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{'type': 'session_meta', 'payload': {'id': sid or self.sid, 'cwd': cwd}}] + rows
        path.write_text('\n'.join(json.dumps(row, ensure_ascii=True) for row in rows))

    def run_cli(self, *args):
        return self.run_command([sys.executable, '-B', str(SCRIPTS / 'pickup.py'), *args])

    def run_command(self, command):
        return subprocess.run(command, env=self.env,
                              text=True, capture_output=True, timeout=15)

    def cursor(self, stdout):
        match = re.search(r'--cursor ([A-Za-z0-9_-]+)', stdout)
        return match.group(1) if match else None

    def content(self, stdout):
        return stdout.split('\n\n', 1)[1].rsplit('\n\n---\nview=', 1)[0]

    def test_huge_unicode_turn_is_bounded_and_reconstructed_via_cli(self):
        text = '上下文🙂\n' * 1800
        self.write([self.message('user', 'request'), self.message('assistant', text)])
        first = self.run_cli('read', self.sid, '--all', '--max-chars', '3000')
        self.assertEqual(first.returncode, 0, first.stderr)
        result, pages = first, []
        while True:
            self.assertLessEqual(len(result.stdout), 3000)
            pages.append(self.content(result.stdout))
            token = self.cursor(result.stdout)
            if not token:
                self.assertIn('complete=yes', result.stdout)
                break
            self.assertIn('complete=no', result.stdout)
            result = self.run_cli('read', self.sid, '--cursor', token, '--max-chars', '3000')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(len(pages), 100)
        meta = pickup.SessionMeta('codex:' + self.sid[-8:], 'codex', self.sid, self.path,
                                  self.path.stat().st_mtime, cwd='/fixture')
        turns, notes = pickup.parse_session(meta)
        expected = pickup.render_read_turns(meta, turns, None, True, None, False, 3000, notes=notes)
        self.assertEqual(''.join(pages), expected)

    def test_changed_source_and_conflicting_cursor_flags_fail_without_content(self):
        self.write([self.message('user', 'request'), self.message('assistant', 'x' * 10000)])
        first = self.run_cli('show', self.sid, '--max-chars', '1800')
        token = self.cursor(first.stdout)
        self.assertIsNotNone(token)
        conflict = self.run_cli('show', self.sid, '--cursor', token, '--tail', '2')
        self.assertNotEqual(conflict.returncode, 0)
        self.assertEqual(conflict.stdout, '')
        with self.path.open('a') as stream:
            stream.write('\n' + json.dumps(self.message('user', 'appended later')))
        changed = self.run_cli('show', self.sid, '--cursor', token)
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(changed.stdout, '')
        self.assertIn('Session changed', changed.stderr)

    def test_restart_command_restores_the_read_view(self):
        self.write([self.message('user', 'request'), self.message('assistant', '很长\n' * 5000)])
        first = self.run_cli('read', self.sid, '--turns', '1', '--with-tools', '--max-bytes', '1500')
        self.assertEqual(first.returncode, 0, first.stderr)
        next_command = shlex.split(first.stdout.splitlines()[-1].removeprefix('Next: '))
        self.write([self.message('user', 'request'), self.message('assistant', '较长\n' * 5000)])
        stale = self.run_command(next_command)
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(stale.stdout, '')
        restart = shlex.split(re.search(r'^Restart with: (.+)$', stale.stderr, re.MULTILINE)[1])
        self.assertEqual(restart[2:], ['read', 'codex:' + self.sid, '--turns', '1', '--with-tools', '--max-bytes', '1500'])
        retried = self.run_command(restart)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn('较长', retried.stdout)
        self.assertLessEqual(len(retried.stdout.encode('utf-8')), 1500)

    def test_stale_cursor_recovers_when_selected_turn_disappears(self):
        first_turn = self.message('user', 'remaining prompt')
        self.write([first_turn, self.message('user', 'removed prompt'),
                    self.message('assistant', 'long reply ' * 500)])
        first = self.run_cli('read', self.sid, '--turns', '2', '--with-tools', '--max-bytes', '1400')
        self.assertEqual(first.returncode, 0, first.stderr)
        token = self.cursor(first.stdout)
        self.assertIsNotNone(token)
        self.write([first_turn])
        stale = self.run_cli('read', self.sid, '--cursor', token, '--max-bytes', '1400')
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(stale.stdout, '')
        self.assertIn('Session changed', stale.stderr)
        self.assertIn('No turns in requested range (2)', stale.stderr)
        restart = shlex.split(re.search(r'^Restart with: (.+)$', stale.stderr, re.MULTILINE)[1])
        self.assertEqual(restart[2:], ['read', 'codex:' + self.sid, '--with-tools', '--max-bytes', '1400'])
        retried = self.run_command(restart)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn('remaining prompt', retried.stdout)

    def test_nonpositive_view_flags_are_argument_errors(self):
        for operation, flag, value in [('show', '--tail', '0'), ('show', '--tail', '-2'),
                                       ('read', '--max-chars', '-2'), ('read', '--max-bytes', '-2'),
                                       ('read', '--max-bytes', '0')]:
            with self.subTest(flag=flag, value=value):
                result = self.run_cli(operation, self.sid, flag, value)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(flag, result.stderr)

    def test_paged_stdout_uses_utf8_even_with_a_different_locale_encoding(self):
        self.write([self.message('user', '本地会话')])
        self.env['PYTHONIOENCODING'] = 'latin-1'
        result = self.run_cli('read', self.sid)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('本地会话', result.stdout)

    def test_overview_and_many_grep_hits_are_bounded(self):
        self.write([self.message(role, text) for _ in range(150)
                    for role, text in [('user', 'needle ' + 'x' * 200), ('assistant', 'needle ' + 'y' * 200)]])
        for args in [('show', self.sid), ('read', self.sid, '--grep', 'needle')]:
            with self.subTest(args=args):
                result = self.run_cli(*args, '--max-chars', '2000')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLessEqual(len(result.stdout), 2000)
                self.assertIn('complete=no', result.stdout)

    def test_display_uid_change_invalidates_cursor(self):
        self.write([self.message('user', 'request'), self.message('assistant', 'x' * 10000)])
        first = self.run_cli('read', self.sid, '--all', '--max-chars', '1800')
        token = self.cursor(first.stdout)
        self.assertIsNotNone(token)
        other = self.path.with_name('collision.jsonl')
        other.write_text(self.path.read_text().replace(self.sid, 'another-session-id'))
        result = self.run_cli('read', self.sid, '--cursor', token)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('Session changed', result.stderr)

    def test_malformed_records_are_partial_but_healthy_matches_survive(self):
        self.write([self.message('user', 'needle')])
        with self.path.open('a') as stream:
            stream.write('\n{invalid json\n[]')
        found = self.run_cli('--agent', 'codex', 'needle')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('coverage: partial', found.stdout)
        self.assertIn('Decode warning', found.stderr)
        absent = self.run_cli('--agent', 'codex', 'missing-query')
        self.assertEqual(absent.returncode, 1)
        self.assertIn('coverage: partial', absent.stdout)
        view = self.run_cli('read', self.sid)
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertIn('Source decoding: partial', view.stdout)

    def test_raw_prefilter_skip_qualifies_json_coverage(self):
        self.write([self.message('user', 'needle')])
        with self.path.open('a') as stream:
            stream.write('\n{"type":}\n')
        missing = self.run_cli('--agent', 'codex', 'strawberry')
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn('prefiltered=1', missing.stderr)
        self.assertIn('readable=0', missing.stderr)
        for output in (missing.stdout, missing.stderr):
            self.assertIn('coverage: complete (raw prefilter skipped JSON validation for some files)', output)
        found = self.run_cli('--agent', 'codex', 'needle')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('Decode warning', found.stderr)
        self.assertIn('coverage: partial', found.stdout)

    def test_antigravity_metadata_failure_is_partial(self):
        root = self.home / '.gemini/antigravity'
        transcript = root / 'brain/fixture/.system_generated/logs/transcript.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type': 'USER_INPUT', 'content': 'needle'}))
        (root / 'conversation_summaries.db').write_bytes(b'bad database')
        result = self.run_cli('--agent', 'antigravity', 'needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('coverage: partial', result.stdout)
        self.assertIn('metadata unavailable', result.stderr)

    def test_filter_skips_other_source_discovery_and_reports_corrupt_database(self):
        db = self.home / '.local/share/devin/cli/sessions.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(b'broken database')
        self.write([self.message('user', 'needle')])
        result = self.run_cli('--agent', 'codex', 'needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('devin', result.stderr)
        self.assertIn('Turn 1', result.stdout)
        bad = self.run_cli('--agent', 'devin')
        self.assertEqual(bad.returncode, 1)
        self.assertIn('Discovery failed', bad.stderr)
        self.assertIn('coverage: partial', bad.stderr)
        self.assertNotIn('broken database', bad.stdout)

    def test_candidate_match_evidence_and_recent_partial_coverage(self):
        self.write([self.message('user', 'request'), self.message('assistant', 'needle original answer')])
        result = self.run_cli('--agent', 'codex', 'needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('assistant, Turn 1', result.stdout)
        self.assertIn('needle original answer', result.stdout)
        self.assertIn('coverage: complete', result.stderr)
        other = self.path.with_name('second.jsonl')
        other.write_text(self.path.read_text().replace(self.sid, 'second-fixture-id'))
        recent = self.run_cli('--agent', 'codex', '--limit', '1')
        self.assertEqual(recent.returncode, 0)
        self.assertIn('coverage: partial', recent.stderr)
        self.assertIn('unscanned=1', recent.stderr)

    def test_lookup_repetition_ranks_below_original_dialogue(self):
        phrase = 'ci-run-4512'
        tool_outputs = [{'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': text}}
                        for text in [f'Earliest output: {phrase}', *['other output'] * 6]]
        fixtures = [
            ('tool', 4, [self.message('user', 'check files'), *tool_outputs]),
            ('lookup', 3, [self.message('user', '[$pickup](/skill/SKILL.md) output from ' + phrase),
                           self.message('assistant', 'Found the source of ' + phrase),
                           self.message('user', 'please keep going'),
                           self.message('user', '<send_user_message_question_reply> details </send_user_message_question_reply>'),
                           self.message('assistant', 'Results from ' + phrase)]),
            ('original', 2, [self.message('user', 'run CI'),
                             self.message('assistant', 'output from ' + phrase)]),
            ('user', 1, [self.message('user', 'Tickets about ' + phrase)]),
        ]
        for name, modified, rows in fixtures:
            path = self.path.with_name(name + '.jsonl')
            self.write(rows, sid=name, path=path)
            os.utime(path, (modified, modified))
        result = self.run_cli('--agent', 'codex', phrase)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = re.findall(r'^\d+\. `codex:([^`]+)`', result.stdout, re.MULTILINE)
        self.assertEqual(candidates, ['original', 'user', 'lookup', 'tool'])
        self.assertIn('match: assistant, Turn 1', result.stdout)
        self.assertIn('match: pickup request, Turn 1', result.stdout)
        match = self.run_cli('read', 'codex:original', '--turns', '1')
        self.assertIn('open match: pickup read codex:original --turns 1', result.stdout)
        self.assertEqual(match.returncode, 0, match.stderr)
        self.assertIn('output from ' + phrase, match.stdout)
        self.assertIn('open match: pickup read codex:tool --grep ' + phrase, result.stdout)
        tool_match = self.run_cli('read', 'codex:tool', '--grep', phrase)
        self.assertEqual(tool_match.returncode, 0, tool_match.stderr)
        self.assertIn('Earliest output: ' + phrase, tool_match.stdout)

    def test_later_unmarked_repost_ranks_below_earlier_assistant_passage(self):
        clue = '布局复核'
        original = (clue + '继续核对生成回执与工作区里的实际记录，并在恢复时确认原始会话、测试结果和最后一轮讨论。'
                    '发布前保留输入参数、对照样例和可复查的结果。')
        copied = original.replace('并在恢复时', '并在\n 恢复时')
        fixtures = [
            ('prior-user', '2025-02-01T09:00:00Z', 4, original, ''),
            ('copied', '2025-02-01T09:10:00Z', 3, copied, ''),
            ('original', '2025-02-01T09:05:00Z', 2, 'please verify the layout', original),
            ('user', '2025-02-01T09:12:00Z', 1, '你之前在哪一轮做过布局复核？', ''),
        ]
        for name, timestamp, modified, user, reply in fixtures:
            path = self.path.with_name(name + '.jsonl')
            rows = [self.message('user', user) | {'timestamp': timestamp}]
            if reply:
                rows.append(self.message('assistant', reply) | {'timestamp': timestamp})
            self.write(rows, sid=name, path=path)
            os.utime(path, (modified, modified))
        result = self.run_cli('--agent', 'codex', clue)
        self.assertEqual(result.returncode, 0, result.stderr)
        ids = re.findall(r'^\d+\. `codex:([^`]+)`', result.stdout, re.MULTILINE)
        self.assertEqual(ids, ['ior-user', 'original', 'user', 'copied'])
        self.assertEqual(result.stdout.count('probable repost'), 1)
        self.assertIn('earlier match: codex:original', result.stdout)

    def test_codex_and_antigravity_subagents_are_openable_and_opt_in_for_keyword_search(self):
        codex_id = '12345678-1234-1234-1234-123456789abc'
        antigravity_id = '98765432-9876-9876-9876-987654321abc'
        marker = 'child-only marker'
        header = {'type': 'session_meta', 'payload': {'id': codex_id, 'source': {'subagent': 'review'}}}
        self.path.write_text('\n'.join(json.dumps(row) for row in (header, self.message('user', marker))))
        root = self.home / '.gemini/antigravity'
        transcript = root / 'brain' / antigravity_id / '.system_generated/logs/transcript_full.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type': 'USER_INPUT', 'content': marker}))
        with closing(sqlite3.connect(root / 'conversation_summaries.db')) as db, db:
            db.execute('CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, '
                       'workspace_uris TEXT, status TEXT, parent_conversation_id TEXT)')
            db.execute("INSERT INTO conversation_summaries VALUES (?, 'child', '', '', 'parent')", (antigravity_id,))

        default = self.run_cli(marker)
        self.assertEqual(default.returncode, 1, default.stdout)
        expanded = self.run_cli('--include-subagents', marker)
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(expanded.stdout.count('| subagent |'), 2)
        for agent, sid, link in [('codex', codex_id, f'codex://threads/{codex_id}'),
                                 ('antigravity', antigravity_id, f'conversation://{antigravity_id}')]:
            with self.subTest(agent=agent):
                exact = self.run_cli(sid)
                self.assertEqual(exact.returncode, 0, exact.stderr)
                self.assertIn('| subagent |', exact.stdout)
                show = self.run_cli('show', link)
                self.assertEqual(show.returncode, 0, show.stderr)
                self.assertIn('kind: subagent session', show.stdout)
                read = self.run_cli('read', f'{agent}:{sid}')
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertIn(marker, read.stdout)
                self.assertIn('complete=yes', read.stdout)

    def test_nested_caller_ids_are_all_excluded_but_direct_read_still_works(self):
        for env_var in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDE_SESSION_ID', 'ANTIGRAVITY_CONVERSATION_ID'):
            sid = env_var.lower().replace('_', '-')
            self.env[env_var] = sid
            self.write([self.message('user', 'shared context marker')], sid=sid,
                       path=self.path.with_name(sid + '.jsonl'))
        self.write([self.message('user', 'shared context marker')])
        found = self.run_cli('--agent', 'codex', 'shared context marker')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('excluded=4', found.stderr)
        self.assertEqual(found.stdout.count('open match:'), 1)
        parent = self.run_cli('read', 'codex:' + self.env['CODEX_SESSION_ID'])
        self.assertEqual(parent.returncode, 0, parent.stderr)
        self.assertIn('shared context marker', parent.stdout)

    def test_antigravity_catalog_only_match_has_an_explicit_read_boundary(self):
        root = self.home / '.gemini/antigravity'
        root.mkdir(parents=True)
        with closing(sqlite3.connect(root / 'conversation_summaries.db')) as db, db:
            db.execute('CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, '
                       'workspace_uris TEXT, status TEXT, parent_conversation_id TEXT, last_modified_time TEXT)')
            db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, '', '', ?)",
                       ('catalog-only', 'Bug from an old session', 'file:///fixture', '2025-02-03T10:11:12Z'))
        found = self.run_cli('--agent', 'antigravity', 'Bug from an old session')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('metadata only | transcript unavailable', found.stdout)
        self.assertIn('coverage: partial', found.stderr)
        self.assertIn('metadata_only=1', found.stderr)
        self.assertIn('readable=0', found.stderr)
        show = self.run_cli('show', 'antigravity:catalog-only')
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('the local transcript is unavailable', show.stdout)
        destination = self.home / 'output'
        for args in [('read', 'antigravity:catalog-only'),
                     ('export', 'antigravity:catalog-only', '--format', 'raw', '-o', str(destination))]:
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('the local transcript is missing', result.stderr)
        self.assertFalse(destination.exists())

    def test_search_filters_use_workspace_record_date_and_recorded_models(self):
        fixtures = [
            ('found', '2025-02-03', '/fixture/archify', ['gpt-astra', 'gpt-terra']),
            ('outside', '2025-02-03', '/fixture/hive', ['gpt-astra']),
            ('old', '2025-02-01', '/fixture/archify', ['gpt-astra']),
            ('other', '2025-02-03', '/fixture/archify', ['gpt-terra']),
        ]
        for name, date, cwd, models in fixtures:
            path = self.path.with_name(name + '.jsonl')
            rows = [{'type': 'turn_context', 'payload': {'model': model}} for model in models]
            self.write(rows + [self.message('user', 'test report')], sid=name, path=path, cwd=cwd)
            modified = datetime.fromisoformat(date).timestamp()
            os.utime(path, (modified, modified))
        result = self.run_cli('--agent', 'codex', '--project', 'ARCHIFY', '--since', '2025-02-03',
                              '--model', 'astra', 'test report')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('codex:found', result.stdout)
        self.assertIn('models=gpt-astra, gpt-terra', result.stdout)
        for name in ('outside', 'old', 'other'):
            self.assertNotIn('codex:' + name, result.stdout)
        self.assertIn('excluded=2, attempted=2', result.stderr)

    def test_claude_jsonl_preserves_unicode_separators_in_user_text(self):
        path = self.home / '.claude/projects/fixture/unicode.jsonl'
        path.parent.mkdir(parents=True)
        user_text = 'first paragraph\u2028second paragraph\u2029needle'
        record = {'type': 'user', 'cwd': '/fixture', 'message': {'content': user_text}}
        path.write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
        result = self.run_cli('--agent', 'claude', 'needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('coverage: complete', result.stderr)
        self.assertIn('match: user, Turn 1', result.stdout)
        read = self.run_cli('read', 'claude:unicode', '--turns', '1')
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn(user_text, read.stdout)

    def test_standalone_skill_injection_not_searchable_but_real_request_survives(self):
        injection = '<skill>\n<name>fixture</name><path>/fixture</path>phantomword\n</skill>'
        rows = [self.message('user', injection), self.message('user', 'real-request'),
                self.message('assistant', 'real-answer')]
        self.write(rows)
        result = self.run_cli('--agent', 'codex', 'phantomword')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('codex:', result.stdout)
        rows[0]['payload']['content'].append({'type': 'input_text', 'text': 'accompanying-request'})
        self.write(rows)
        result = self.run_cli('--agent', 'codex', 'accompanying-request')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('user, Turn 1', result.stdout)

    def test_attached_request_is_read_and_searchable_without_attachment_header(self):
        request = self.message('user', '# Files mentioned by the user:\n\n## Sample file\n'
                               'attachment-metadata\n\n## My request:\n截图里是哪段会话？')
        request['payload']['content'].extend([
            {'type': 'input_text', 'text': '<image name="Image #1" path="/tmp/attachment file.png">'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,secret-bytes'},
            {'type': 'input_text', 'text': '</image>'},
        ])
        self.write([request, self.message('assistant', '这是原始日志')])
        found = self.run_cli('--agent', 'codex', '截图里是哪段会话')
        self.assertEqual(found.returncode, 0, found.stderr)
        view = self.run_cli('read', self.sid)
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertIn('**User**:\n截图里是哪段会话？', view.stdout)
        self.assertIn('[Image attachment: /tmp/attachment file.png]', view.stdout)
        self.assertNotIn('secret-bytes', view.stdout)
        self.assertNotIn('attachment-metadata', view.stdout)

    def test_same_codex_id_in_active_and_archive_has_a_round_trip_selector(self):
        self.sid = '12345678-1234-1234-1234-123456789abc'
        self.write([self.message('user', 'other record')])
        self.path = self.home / '.codex/archived_sessions/other.jsonl'
        self.write([self.message('user', 'archived request'), self.message('assistant', 'original needle' + 'z' * 4000)])
        found = self.run_cli('--agent', 'codex', 'original needle')
        self.assertEqual(found.returncode, 0, found.stderr)
        uid = re.search(r'`(codex:[^ `]+@[^ `]+)`', found.stdout)[1]
        self.assertIn('source=archived_sessions', found.stdout)
        show = self.run_cli('show', uid)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('archived request', show.stdout)
        view = self.run_cli('read', uid, '--max-chars', '2000')
        self.assertEqual(view.returncode, 0, view.stderr)
        resume = self.run_cli('read', uid, '--cursor', self.cursor(view.stdout))
        self.assertEqual(resume.returncode, 0, resume.stderr)
        self.assertIn('complete=yes', resume.stdout)
        ambiguous = self.run_cli('show', f'codex://threads/{self.sid}')
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn(uid, ambiguous.stderr)

    @unittest.skipUnless(pickup.zstd is not None or shutil.which('zstd'), 'Zstandard reader unavailable')
    def test_archive_compressed_link_and_file_exports(self):
        self.sid = '12345678-1234-1234-1234-123456789abc'
        self.path = self.home / '.codex/archived_sessions/archived.jsonl'
        ending = 'late-dialogue-marker'
        self.write([self.message('user', 'archived request'),
                    self.message('assistant', '长对话\n' * 4000 + ending)])
        packed = self.path.with_suffix('.jsonl.zst')
        if pickup.zstd is not None:
            packed.write_bytes(pickup.zstd.compress(self.path.read_bytes()))
        else:
            subprocess.run(['zstd', '-q', str(self.path)], check=True, timeout=10)
        self.path.unlink()
        link = f'[Archived chat](codex://threads/{self.sid})'
        candidate = self.run_cli(link)
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        self.assertIn('session identifier matched', candidate.stdout)
        markdown = self.home / 'dialogue 中文.md'
        out = self.run_cli('export', link, '--output', str(markdown))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(ending, markdown.read_text())
        raw = self.home / 'record.jsonl.zst'
        out = self.run_cli('export', link, '--format', 'raw', '-o', str(raw))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(raw.read_bytes(), packed.read_bytes())
        collision = self.run_cli('export', link, '--format', 'raw', '-o', str(markdown))
        self.assertNotEqual(collision.returncode, 0)
        self.assertIn(ending, markdown.read_text())

    @unittest.skipUnless(pickup.zstd is not None or shutil.which('zstd'), 'Zstandard fixture encoder unavailable')
    def test_older_python_scans_multiple_zstd_files_with_the_shared_library(self):
        if pickup.zstd_library() is None:
            self.skipTest('libzstd unavailable')

        def frame(data):
            if pickup.zstd is not None:
                return pickup.zstd.compress(data)
            return subprocess.run(['zstd', '-qc'], input=data, check=True,
                                  stdout=subprocess.PIPE, timeout=10).stdout

        for sid in ('compressed-hit', 'compressed-miss'):
            path = self.path.with_name(sid + ' - 长 [$].jsonl')
            text = 'unique compressed request' if sid.endswith('hit') else 'other prompt'
            body = [self.message('user', text), self.message('assistant', 'reply ' * 100_000)]
            self.write(body, sid=sid, path=path)
            header, dialogue = path.read_bytes().split(b'\n', 1)
            path.with_suffix('.jsonl.zst').write_bytes(frame(header + b'\r') + frame(dialogue))
            path.unlink()

        with patch.object(pickup, 'HOME', self.home), patch.object(pickup, 'zstd', None), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('spawned a zstd subprocess')):
            sessions = pickup.discover_codex()
            self.assertEqual({item.raw_id for item in sessions}, {'compressed-hit', 'compressed-miss'})
            found = pickup.find_matching_sessions(sessions, 'unique compressed request')
            self.assertEqual([meta.raw_id for meta, _, _ in found], ['compressed-hit'])
            packed = found[0][0].path
            self.assertEqual(found[0][1][0].assistant_texts, ['reply ' * 99_999 + 'reply'])
            packed.write_bytes(packed.read_bytes()[:-1])
            with pickup.open_codex_log(packed, binary=True) as stream, self.assertRaisesRegex(OSError, 'Truncated'):
                stream.read()

    def test_export_selected_ranges_omits_unrelated_turns_and_deduplicates(self):
        self.write([self.message('user', f'question-{i}') for i in range(1, 7)])
        out = self.home / 'selected.md'
        result = self.run_cli('export', self.sid, '--turns', '5-6', '--turns', '2', '--turns', '6',
                              '--output', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = out.read_text()
        self.assertEqual(re.findall(r'^### Turn (\d+)', text, re.MULTILINE), ['2', '5', '6'])
        self.assertNotIn('question-3', text)
        bad = self.home / 'invalid.md'
        result = self.run_cli('export', self.sid, '--turns', '99', '--output', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(bad.exists())
        self.assertIn('this session has 6 turns', result.stderr)
        result = self.run_cli('export', self.sid, '--turns', '5-7', '--output', str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(bad.exists())
        self.assertIn('exceeds the last turn (6)', result.stderr)

    def test_broken_source_is_partial_and_bad_cursor_has_no_output(self):
        self.write([self.message('user', 'needle')])
        pi = self.home / '.pi/agent/sessions/project'
        pi.mkdir(parents=True)
        (pi / 'broken.jsonl').write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session', 'id': 'broken'},
            {'type': 'message', 'id': 'a', 'parentId': 'missing', 'message': {'role': 'user', 'content': []}}]))
        result = self.run_cli('needle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('coverage: partial', result.stdout)
        self.assertIn('failed=1', result.stderr)
        bad = self.run_cli('read', self.sid, '--cursor', 'not-json')
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, '')

    def test_codex_prefilter_keeps_escaped_text_and_words_across_tool_fields(self):
        stamp = '2025-04-01T09:00:00Z'
        rows = [dict(type='session_meta', payload={'id': self.sid}), self.message('user', 'request'),
                self.message('assistant', '一些本地会话和 "QUOTED"\npath\\name')]
        rows.extend(dict(type='response_item', payload={'type': 'function_call_output', 'output': text})
                    for text in ('violet', 'raspberry', [{'nested': 'nonprinting \u00adhello'}]))
        self.path.write_text('\n'.join(json.dumps({'timestamp': stamp, **row}, separators=(',', ':')) for row in rows))
        for query, evidence in [('本地会话', 'assistant, Turn 1'), ('"quoted"\npath\\name', 'assistant, Turn 1'),
                                ('violet raspberry', 'words matched across fields'),
                                ('xadhello', 'tool output, Turn 1')]:
            with self.subTest(query=query):
                result = self.run_cli('--agent', 'codex', query)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(evidence, result.stdout)
        missing = self.run_cli('--agent', 'codex', 'strawberry')
        self.assertEqual(missing.returncode, 1, missing.stderr)
        # The Unicode-escaped file defers to the decoder. A literal-only file
        # can be ruled out without parsing it.
        self.path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows[:2]))
        missing = self.run_cli('--agent', 'codex', 'strawberry')
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn('prefiltered=1', missing.stderr)

    def test_codex_prefilter_keeps_records_after_ignored_cr_delimited_event(self):
        rows = [{'type': 'session_meta', 'payload': {'id': self.sid}},
                {'type': 'event_msg', 'payload': {'type': 'notice'}},
                self.message('user', '本地会话')]
        header, event, message = [json.dumps({'timestamp': '2025-04-01T09:00:00Z', **row},
                                             ensure_ascii=False, separators=(',', ':')).encode() for row in rows]
        self.path.write_bytes(header + b'\n' + event + b'\r' + message + b'\r\n')
        result = self.run_cli('--agent', 'codex', '本地会话')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('user, Turn 1', result.stdout)
        read = self.run_cli('read', self.sid, '--grep', '本地会话')
        self.assertIn('本地会话', read.stdout)

    def test_explicit_source_citation_and_directory_listing_rank_below_reply(self):
        for sid, rows, stamp in [
            ('original', [self.message('user', 'check the run'), self.message('assistant', 'CI run 44968716810')], 1),
            ('citation', [self.message('user', 'Verify that codex:original still ranks first for 44968716810')], 2),
            ('listing', [self.message('user', 'list the output directory'),
                         dict(type='response_item', payload=dict(type='function_call_output', output='report-44968716810.md'))], 3),
        ]:
            path = self.path.with_name(sid + '.jsonl')
            self.write([{'timestamp': f'2025-04-0{stamp}T09:00:00Z', **row} for row in rows], path=path, sid=sid)
            os.utime(path, (stamp, stamp))
        result = self.run_cli('44968716810')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(re.findall(r'^\d+\. \x60codex:([^\x60]+)', result.stdout, re.MULTILINE),
                         ['original', 'citation', 'listing'])
        self.assertIn('citing an earlier match', result.stdout)

    def write_antigravity_index(self, session_ids, chunks):
        root = self.home / '.gemini/antigravity'
        root.mkdir(parents=True)
        db_path = root / 'conversation_summaries.db'
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE conversation_summaries '
                       '(conversation_id, title, workspace_uris, status, parent_conversation_id)')
            db.executemany("INSERT INTO conversation_summaries VALUES (?, 'indexed fixture', '', '', '')",
                           [(sid,) for sid in session_ids])
            columns = ('chunk_index, chunk_first_step, user_step_byte_offsets, agent_step_byte_offsets, user_body, agent_body')
            if db.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]:
                db.execute(f'CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, {columns})')
            else:
                db.execute(f'CREATE TABLE conversations_fts (conversation_id, {columns})')
            db.executemany('INSERT INTO conversations_fts VALUES (?, ?, ?, ?, ?, ?, ?)', chunks)
        return db_path

    def test_antigravity_fts_fallback_preserves_order_and_labels_fidelity(self):
        sid = 'indexed-antigravity'
        db_path = self.write_antigravity_index([sid], [
            (sid, 0, 1, b'[[1,0],[3,7]]', b'[[2,0],[4,13]]', '一问\n二问', '独特回答\n后续'),
        ])
        view = self.run_cli('read', 'antigravity:' + sid, '--all')
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertIn('SQLite indexed text', view.stdout)
        self.assertLess(view.stdout.index('独特回答'), view.stdout.index('二问'))
        self.assertIn('后续', view.stdout)
        show = self.run_cli('show', 'antigravity:' + sid)
        self.assertIn('fidelity:', show.stdout)
        search = self.run_cli('--agent', 'antigravity', '独特回答')
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn('assistant, Turn 1', search.stdout)
        self.assertIn('indexed_text=1', search.stderr)
        destination = self.home / 'indexed.md'
        export = self.run_cli('export', 'antigravity:' + sid, '--output', str(destination))
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertIn('conversations_fts', destination.read_text())
        raw = self.run_cli('export', 'antigravity:' + sid, '--format', 'raw', '--output', str(self.home / 'raw'))
        self.assertEqual(raw.returncode, 2)
        self.assertIn('Use the Markdown export', raw.stderr)
        with closing(pickup.pickup_extra.open_readonly_db(db_path)) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                db.execute('DELETE FROM conversation_summaries')

    def test_antigravity_cli_native_logs_win_over_index_without_duplicate_ids(self):
        ids = ['cli-main', 'cli-child', 'shared-id']
        db_path = self.write_antigravity_index(ids, [
            (sid, 0, 1, '[[1,0]]', '[]', 'stale indexed marker', '') for sid in ids
        ])
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE conversation_summaries SET parent_conversation_id='cli-main' "
                       "WHERE conversation_id='cli-child'")

        def transcript(store, sid, filename, text):
            path = self.home / '.gemini' / store / 'brain' / sid / '.system_generated/logs' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'type': 'USER_INPUT', 'content': text}))
            return path

        transcript('antigravity-cli', 'cli-main', 'transcript.jsonl', 'short CLI marker')
        transcript('antigravity-cli', 'cli-main', 'transcript_full.jsonl', 'full CLI marker')
        transcript('antigravity-cli', 'cli-child', 'transcript.jsonl', 'child CLI marker')
        transcript('antigravity-cli', 'shared-id', 'transcript_full.jsonl', 'duplicate CLI marker')
        transcript('antigravity', 'shared-id', 'transcript.jsonl', 'preferred desktop marker')
        result = self.run_cli('--agent', 'antigravity', 'full CLI marker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('discovered=2', result.stderr)
        self.assertIn('indexed_text=0', result.stderr)
        self.assertEqual(self.run_cli('--agent', 'antigravity', 'child CLI marker').returncode, 1)
        expanded = self.run_cli('--include-subagents', 'child CLI marker')
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        child = self.run_cli('show', 'antigravity:cli-child')
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('kind: subagent session', child.stdout)
        self.assertIn('child CLI marker', child.stdout)
        shared = self.run_cli('read', 'antigravity:shared-id')
        self.assertIn('preferred desktop marker', shared.stdout)
        self.assertNotIn('duplicate CLI marker', shared.stdout)

    def test_antigravity_cli_catalog_works_without_desktop_store(self):
        db_path = self.write_antigravity_index(['cli-child'], [])
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE conversation_summaries SET parent_conversation_id='parent'")
        root = self.home / '.gemini/antigravity-cli'
        db_path.parent.rename(root)
        path = root / 'brain/cli-child/.system_generated/logs/transcript.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'USER_INPUT', 'content': 'CLI-only child marker'}))
        sid = 'antigravity:cli-child'
        self.assertEqual(self.run_cli('--agent', 'antigravity', 'CLI-only child marker').returncode, 1)
        self.assertIn('CLI-only child marker', self.run_cli('read', sid).stdout)

    def test_antigravity_whitespace_index_is_still_searchable_metadata(self):
        sid = 'whitespace-index'
        self.write_antigravity_index([sid], [(sid, 0, 1, '[[1,0]]', None, '\n', '\u2003\t')])
        for query in ('indexed fixture', 'antigravity:' + sid):
            with self.subTest(query=query):
                result = self.run_cli('--agent', 'antigravity', query)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('metadata only', result.stdout)
                self.assertIn('metadata_only=1', result.stderr)
                self.assertIn('readable=0', result.stderr)
        show = self.run_cli('show', 'antigravity:' + sid)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('view_scope: metadata only', show.stdout)

    def test_antigravity_keyword_search_reads_the_index_in_one_pass(self):
        ids = ['indexed-one', 'indexed-two', 'with-transcript']
        db_path = self.write_antigravity_index(ids, [
            (sid, 0, 1, '[[1,0]]', '[[2,0]]', 'question', f'needle in {sid}') for sid in ids
        ])
        raw = self.home / '.gemini/antigravity/brain/with-transcript/.system_generated/logs/transcript_full.jsonl'
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({'type': 'USER_INPUT', 'content': 'different raw transcript'}))
        statements = []
        open_db = pickup.pickup_extra.open_readonly_db

        def traced_open(path):
            db = open_db(path)
            db.set_trace_callback(statements.append)
            return db

        output = io.StringIO()
        with patch.object(pickup, 'HOME', self.home), patch.object(pickup.pickup_extra, 'open_readonly_db', traced_open), \
                patch.object(sys, 'argv', ['pickup', '--agent', 'antigravity', 'needle']), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(pickup.main(), 0)
        self.assertIn('needle in indexed-one', output.getvalue())
        self.assertIn('needle in indexed-two', output.getvalue())
        self.assertNotIn('needle in with-transcript', output.getvalue())
        index_reads = [sql for sql in statements if re.search(r'\bFROM conversations_fts\b', sql, re.IGNORECASE)]
        with closing(open_db(db_path)) as db:
            plans = [detail for sql in index_reads for *_, detail in db.execute('EXPLAIN QUERY PLAN ' + sql)]
        full_scans = [plan for plan in plans if re.fullmatch(r'SCAN conversations_fts(?: VIRTUAL TABLE INDEX \d+:)?', plan)]
        self.assertLessEqual(len(full_scans), 1, plans)

    def test_codex_sqlite_orphan_and_rollout_priority(self):
        sid = 'sqlite-only-session'
        state = self.home / '.codex/state_5.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads (id, rollout_path, cwd, title, source, updated_at, model)')
            db.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (sid, str(self.path.with_name('missing.jsonl')), '/fixture', 'sqlite fixture', 'exec', 10, 'fixture-model'))
        history = self.home / '.codex/thread_history_1.sqlite'
        with closing(sqlite3.connect(history)) as db, db:
            db.execute('CREATE TABLE thread_items (thread_id, item_type, item_json, created_at_ms, rollout_ordinal, item_id)')
            for thread in (sid, self.sid):
                for ordinal, (item_type, item) in enumerate([
                    ('userMessage', {'content': [{'type': 'text', 'text': 'projected prompt'}]}),
                    ('agentMessage', {'text': 'unique projected reply'}),
                    ('reasoning', {'text': 'hidden reasoning'}),
                ]):
                    db.execute('INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)',
                               (thread, item_type, json.dumps(item), 1_750_000_000_000, ordinal, str(ordinal)))
        self.write([self.message('user', 'preferred rollout')])
        show = self.run_cli('show', 'codex:' + sid)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn('SQLite projected dialogue', show.stdout)
        read = self.run_cli('read', 'codex:' + sid, '--turns', '1')
        self.assertIn('unique projected reply', read.stdout)
        self.assertNotIn('hidden reasoning', read.stdout)
        self.assertIn('preferred rollout', self.run_cli('read', self.sid).stdout)
        search = self.run_cli('--agent', 'codex', '--model', 'fixture-model', 'unique projected reply')
        self.assertEqual(search.returncode, 0, search.stderr)
        self.assertIn('sqlite_history=1', search.stderr)
        self.assertIn('assistant, Turn 1', search.stdout)
        destination = self.home / 'sqlite.md'
        export = self.run_cli('export', 'codex:' + sid, '--output', str(destination))
        self.assertEqual(export.returncode, 0, export.stderr)
        self.assertIn('thread_items', destination.read_text())
        raw = self.run_cli('export', 'codex:' + sid, '--format', 'raw', '--output', str(self.home / 'raw'))
        self.assertEqual(raw.returncode, 2)
        self.assertIn('lacks raw events', raw.stderr)

    def test_codex_catalog_loads_external_rollouts_and_falls_back_if_unreadable(self):
        state = self.home / '.codex/state_5.sqlite'
        history = self.home / '.codex/thread_history_1.sqlite'
        fixtures = [('external-only', None), ('external-preferred', 'stale history'),
                    ('external-corrupt', 'recovered history'), ('external-body-corrupt', 'recovered body')]
        with closing(sqlite3.connect(state)) as catalog, catalog, closing(sqlite3.connect(history)) as db, db:
            catalog.execute('CREATE TABLE threads (id, rollout_path)')
            db.execute('CREATE TABLE thread_items (thread_id, item_type, item_json, created_at_ms, rollout_ordinal, item_id)')
            for sid, fallback in fixtures:
                path = self.home / (sid + '.jsonl')
                if sid == 'external-corrupt':
                    path.write_text('broken log')
                elif sid == 'external-body-corrupt':
                    self.write([], sid=sid, path=path)
                    with path.open('a') as stream:
                        stream.write('\nbroken record')
                else:
                    self.write([self.message('user', sid + ' marker')], sid=sid, path=path)
                catalog.execute('INSERT INTO threads VALUES (?, ?)', (sid, str(path)))
                if fallback:
                    db.execute('INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)',
                               (sid, 'agentMessage', json.dumps({'text': fallback}), 1_750_000_000_000, 0, '0'))
        for sid, fallback in fixtures:
            with self.subTest(sid=sid):
                show = self.run_cli('show', 'codex:' + sid)
                self.assertEqual(show.returncode, 0, show.stderr)
                if 'corrupt' in sid:
                    self.assertIn('SQLite projected dialogue', show.stdout)
                    self.assertIn(fallback, show.stdout)
                else:
                    self.assertIn(str(self.home / (sid + '.jsonl')), show.stdout)
                    self.assertIn(sid + ' marker', show.stdout)
                    self.assertNotIn('SQLite projected dialogue', show.stdout)
        found = self.run_cli('--agent', 'codex', 'external-preferred marker')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('user, Turn 1', found.stdout)

    def test_codex_sqlite_image_only_prompts_keep_their_own_replies(self):
        sid = 'images-only-fixture'
        history = self.home / '.codex/thread_history_1.sqlite'
        messages = [
            ('userMessage', {'content': [
                {'type': 'text', 'text': '<environment_context>discard this context</environment_context>'},
                {'type': 'text', 'text': 'original text question'},
                {'type': 'localImage', 'path': '/fixture/first.png'},
            ]}),
            ('agentMessage', {'text': 'original text answer'}),
            ('userMessage', {'content': [{'type': 'localImage', 'path': '/fixture/photo.png'}]}),
            ('agentMessage', {'text': 'local image answer'}),
            ('userMessage', {'content': [{'type': 'image', 'url': 'data:image/png;base64,private-bytes'}]}),
            ('agentMessage', {'text': 'remote image answer'}),
        ]
        with closing(sqlite3.connect(history)) as db, db:
            db.execute('CREATE TABLE thread_items (thread_id, item_type, item_json, created_at_ms, rollout_ordinal, item_id)')
            db.executemany('INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)', [
                (sid, kind, json.dumps(item), 1_750_000_000_000, index, str(index))
                for index, (kind, item) in enumerate(messages)
            ])
        first = self.run_cli('read', 'codex:' + sid, '--turns', '1')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('original text question', first.stdout)
        self.assertIn('[Image attachment: /fixture/first.png]', first.stdout)
        self.assertNotIn('discard this context', first.stdout)
        found = self.run_cli('--agent', 'codex', 'original text question')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn('user, Turn 1', found.stdout)
        for turn, image, reply in [('2', '[Image attachment: /fixture/photo.png]', 'local image answer'),
                                   ('3', '[Image attachment]', 'remote image answer')]:
            with self.subTest(turn=turn):
                result = self.run_cli('read', 'codex:' + sid, '--turns', turn)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(image, result.stdout)
                self.assertIn(reply, result.stdout)
                self.assertNotIn('original text answer', result.stdout)
                self.assertNotIn('private-bytes', result.stdout)

    def test_nested_subagents_open_directly_and_opt_in_for_search_and_list(self):
        sid = 'agent-ageneral-purpose-fixture'
        for agent in ('qoder', 'claude'):
            with self.subTest(agent=agent):
                path = self.home / f'.{agent}/projects/project/parent/subagents/{sid}.jsonl'
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'type': 'user', 'message': {'content': 'subagent unique marker'}}))
                uid = agent + ':' + sid
                self.assertIn('kind: subagent session', self.run_cli('show', uid).stdout)
                self.assertIn('subagent unique marker', self.run_cli('read', uid).stdout)
                exported = self.home / f'{agent}.md'
                self.assertEqual(self.run_cli('export', uid, '--output', str(exported)).returncode, 0)
                self.assertIn('subagent unique marker', exported.read_text())
                self.assertNotIn(uid, self.run_cli('--agent', agent).stdout)
                self.assertNotIn(uid, self.run_cli('--agent', agent, 'subagent unique marker').stdout)
                self.assertIn(uid, self.run_cli('--agent', agent, '--include-subagents').stdout)
                found = self.run_cli('--agent', agent, '--include-subagents', 'subagent unique marker')
                self.assertEqual(found.returncode, 0, found.stderr)
                self.assertIn(uid, found.stdout)

    def test_kimi_subagent_ids_are_scoped_and_open_the_selected_wire_log(self):
        for root, sid, marker in (('.kimi-code', 'parent-one', 'first unique child'),
                                  ('.kimi-test', 'parent-two', 'second unique child')):
            session = self.home / root / 'sessions' / 'wd_project' / ('session_' + sid)
            for agent, text in (('main', 'main prompt'), ('agent-0', marker)):
                wire = session / 'agents' / agent / 'wire.jsonl'
                wire.parent.mkdir(parents=True)
                wire.write_text(json.dumps({'type': 'agent.message.appended',
                                            'message': {'message': {'role': 'user', 'content': text}}}))
            (session / 'state.json').write_text(json.dumps({'id': sid, 'cwd': '/fixture', 'title': 'Kimi fixture'}))
        uid = 'kimi:parent-two/agent-0'
        default = self.run_cli('--agent', 'kimi')
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertNotIn('/agent-0', default.stdout)
        self.assertEqual(self.run_cli('--agent', 'kimi', 'second unique child').returncode, 1)
        listed = self.run_cli('--agent', 'kimi', '--include-subagents')
        self.assertIn('kimi:parent-one/agent-0', listed.stdout)
        self.assertIn(uid, listed.stdout)
        found = self.run_cli('--agent', 'kimi', '--include-subagents', 'second unique child')
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn(uid, found.stdout)
        self.assertNotIn('first unique child', found.stdout)
        show = self.run_cli('show', uid)
        self.assertIn('kind: subagent session', show.stdout)
        self.assertIn('second unique child', show.stdout)
        read = self.run_cli('read', 'parent-two/agent-0')
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn('second unique child', read.stdout)
        for form in ('raw', 'markdown'):
            dest = self.home / f'child-{form}'
            exported = self.run_cli('export', uid, '--format', form, '-o', str(dest))
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertIn('second unique child', dest.read_text())
            if form == 'raw':
                wire = self.home / '.kimi-test/sessions/wd_project/session_parent-two/agents/agent-0/wire.jsonl'
                self.assertEqual(dest.read_bytes(), wire.read_bytes())

    def test_locked_sqlite_history_leaves_rollout_readable(self):
        external = self.home / 'elsewhere.jsonl'
        self.write([self.message('user', 'rollout still readable')], path=external)
        with closing(sqlite3.connect(self.home / '.codex/state_5.sqlite')) as catalog, catalog:
            catalog.execute('CREATE TABLE threads (id, rollout_path)')
            catalog.execute('INSERT INTO threads VALUES (?, ?)', (self.sid, str(external)))
        with closing(sqlite3.connect(self.home / '.codex/thread_history_1.sqlite')) as db:
            db.execute('BEGIN EXCLUSIVE')
            db.execute('CREATE TABLE thread_items (thread_id)')
            result = self.run_cli('read', 'codex:' + self.sid)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('rollout still readable', result.stdout)
            self.assertIn('SQLite history unavailable', result.stderr)

    def test_qoder_active_leaf_and_historical_turn_references(self):
        path = self.home / '.qoder/projects/project/branch-fixture.jsonl'
        path.parent.mkdir(parents=True)

        def row(role, uuid, parent, text):
            content = text if role == 'user' else [{'type': 'text', 'text': text}]
            return dict(type=role, uuid=uuid, parentUuid=parent, message={'content': content})

        rows = [row('user', 'u', None, 'original question'),
                row('assistant', 'old', 'u', 'historical answer'),
                row('assistant', 'new', 'u', 'active answer'),
                {'type': 'active-leaf', 'leafUuid': 'new'},
                row('user', 'abandoned', 'old', 'historical followup'),
                row('assistant', 'last', 'abandoned', 'another abandoned answer')]
        del rows[0]['parentUuid']  # Roots can omit the link.
        path.write_text('\n'.join(json.dumps(record) for record in rows))
        uid = 'qoder:branch-fixture'
        for operation in ('read', 'show'):
            active = self.run_cli(operation, uid)
            self.assertEqual(active.returncode, 0, active.stderr)
            self.assertIn('active answer', active.stdout)
            self.assertNotIn('historical answer', active.stdout)
            self.assertNotIn('historical followup', active.stdout)
        history = self.run_cli('read', uid, '--grep', 'historical answer')
        self.assertIn('Turn 1', history.stdout)
        self.assertIn('[off-branch]', history.stdout)
        found = self.run_cli('historical answer')
        self.assertIn('assistant, Turn 1 (off-branch)', found.stdout)
        command = re.search(r'open match: pickup (.+)', found.stdout)
        self.assertIsNotNone(command, found.stdout)
        opened = self.run_cli(*shlex.split(command[1]))
        self.assertIn('historical answer', opened.stdout)
        self.assertIn('active answer', opened.stdout)
        self.assertNotIn('historical followup', opened.stdout)
        self.assertIn('historical followup', self.run_cli('read', uid, '--all-branches', '--turns', '2').stdout)

    def write_qoder_graph(self, sid, rows):
        path = self.home / '.qoder/projects/project' / (sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(map(json.dumps, rows)))
        return 'qoder:' + sid

    @staticmethod
    def qoder_row(role, uuid, parent, content):
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        return dict(type=role, uuid=uuid, parentUuid=parent, message={'content': content})

    def test_qoder_active_history_crosses_compact_boundaries(self):
        row = self.qoder_row
        uid = self.write_qoder_graph('compacted', [
            row('user', 'u', None, 'original prompt before compaction'),
            row('assistant', 'a', 'u', 'original answer before compaction'),
            row('assistant', 'old', 'u', 'abandoned answer before compaction'),
            {'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'compact', 'logicalParentUuid': 'a'},
            row('user', 'next', 'compact', 'prompt after compaction'),
            row('assistant', 'end', 'next', 'answer after compaction'),
            {'type': 'active-leaf', 'leafUuid': 'end'},
        ])
        destination = self.home / 'compacted.md'
        outputs = []
        for args in [('show', uid), ('read', uid, '--all'), ('export', uid, '-o', str(destination))]:
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(destination.read_text() if args[0] == 'export' else result.stdout)
        for output in outputs:
            self.assertIn('original prompt before compaction', output)
            self.assertIn('original answer before compaction', output)
            self.assertIn('answer after compaction', output)
            self.assertNotIn('abandoned answer before compaction', output)
        hit = self.run_cli('read', uid, '--grep', 'original answer before compaction')
        self.assertIn('Turn 1', hit.stdout)
        self.assertNotIn('[off-branch]', hit.stdout)

    def test_qoder_parallel_tool_results_stay_with_the_active_turn(self):
        row = self.qoder_row

        def call(uuid, parent):
            return row('assistant', uuid, parent, [{'type': 'tool_use', 'id': uuid, 'name': 'Lookup'}])

        def result(uuid, parent, call_id, text):
            return row('user', uuid, parent, [{'type': 'tool_result', 'tool_use_id': call_id, 'content': text}])

        call_ids = [f'tool-{number}' for number in range(1, 7)]
        outputs = [f'parallel marker {number}' for number in range(1, 7)]
        calls = [call(uuid, parent) for uuid, parent in zip(call_ids, ['u', *call_ids])]
        results = [result(f'result-{uuid}', uuid, uuid, text) for uuid, text in zip(call_ids, outputs)]
        uid = self.write_qoder_graph('parallel', [
            row('user', 'u', None, 'lookup question'),
            *calls, *results,
            row('assistant', 'end', 'result-tool-6', 'active reply'),
            result('other', 'tool-1', 'unknown-tool', 'unrelated tool marker'),
            row('assistant', 'old', 'tool-1', 'abandoned assistant reply'),
            result('behind-fork', 'old', 'tool-1', 'tool marker behind an abandoned reply'),
            row('user', 'followup', 'tool-1', 'abandoned user reply'),
            {'type': 'active-leaf', 'leafUuid': 'end'},
        ])
        destination = self.home / 'parallel.md'
        view = self.run_cli('read', uid, '--with-tools')
        self.assertEqual(view.returncode, 0, view.stderr)
        exported = self.run_cli('export', uid, '--with-tools', '-o', str(destination))
        self.assertEqual(exported.returncode, 0, exported.stderr)
        for output in [view.stdout, destination.read_text()]:
            self.assertEqual(re.findall(r'^parallel marker \d+$', output, re.MULTILINE), outputs)
            self.assertNotIn('unrelated tool marker', output)
            self.assertNotIn('tool marker behind an abandoned reply', output)
            self.assertNotIn('abandoned assistant reply', output)
            self.assertNotIn('abandoned user reply', output)
        hit = self.run_cli('read', uid, '--grep', 'parallel marker')
        self.assertEqual(re.findall(r'^parallel marker \d+$', hit.stdout, re.MULTILINE), outputs)
        self.assertEqual(hit.stdout.count('### Turn 1 '), 1)
        self.assertNotIn('[off-branch]', hit.stdout)

    def test_qoder_late_parallel_result_is_attached_to_its_call_turn(self):
        row = self.qoder_row
        uid = self.write_qoder_graph('late-result', [
            row('user', 'first', None, 'first prompt'),
            row('assistant', 'call', 'first', [
                {'type': 'tool_use', 'id': 'quick', 'name': 'Read'},
                {'type': 'tool_use', 'id': 'slow', 'name': 'Read'},
            ]),
            row('user', 'quick-result', 'call', [
                {'type': 'tool_result', 'tool_use_id': 'quick', 'content': 'quick output'},
            ]),
            row('user', 'second', 'quick-result', 'second prompt'),
            row('user', 'slow-result', 'call', [
                {'type': 'tool_result', 'tool_use_id': 'slow', 'content': 'late sibling output'},
            ]),
            row('assistant', 'end', 'second', 'second reply'),
            {'type': 'active-leaf', 'leafUuid': 'end'},
        ])
        first = self.run_cli('read', uid, '--turns', '1', '--with-tools')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('late sibling output', first.stdout)
        second = self.run_cli('read', uid, '--turns', '2', '--with-tools')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('second reply', second.stdout)
        self.assertNotIn('late sibling output', second.stdout)


if __name__ == '__main__':
    unittest.main()
