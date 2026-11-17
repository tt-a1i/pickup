"""Exercise the public CLI in isolated homes; never read personal histories."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import pickup


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {**os.environ, 'HOME': str(self.home)}
        self.sid = 'fixture-session-id'
        self.path = self.home / '.codex/sessions/fixture.jsonl'
        self.path.parent.mkdir(parents=True)

    def message(self, role, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}

    def write(self, rows):
        rows = [{'type': 'session_meta', 'payload': {'id': self.sid, 'cwd': '/fixture'}}] + rows
        self.path.write_text('\n'.join(json.dumps(row, ensure_ascii=True) for row in rows))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / 'pickup.py'), *args], env=self.env,
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


if __name__ == '__main__':
    unittest.main()
