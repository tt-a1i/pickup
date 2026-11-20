"""Exercise the public CLI in isolated homes; never read personal histories."""
import json
import os
from pathlib import Path
import re
import shutil
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
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


if __name__ == '__main__':
    unittest.main()
