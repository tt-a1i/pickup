import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('pickup', SCRIPTS / 'pickup.py')
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


class SelectionTests(unittest.TestCase):
    def meta(self, sid='target', mtime=1):
        return p.SessionMeta('codex:' + sid, 'codex', sid, Path('/unused'), mtime)

    def test_no_query_lists_without_loading_overview(self):
        m = self.meta()
        turn = p.Turn(1, '', 'a real prior request')
        output = io.StringIO()
        with patch.object(sys, 'argv', ['pickup']), patch.object(p, 'discover_all', return_value=[m]), patch.object(p, 'parse_session', return_value=([turn], [])), contextlib.redirect_stdout(output):
            self.assertEqual(p.main(), 0)
        self.assertIn('codex:target', output.getvalue())
        self.assertNotIn('All User Prompts Outline', output.getvalue())

    def test_exact_caller_excluded_but_other_pickup_history_kept(self):
        own, prior = self.meta('own', 2), self.meta('prior')
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'own'}, clear=True), patch.object(p, 'parse_session', return_value=([p.Turn(1, '', '/pickup')], [])):
            found = p.find_matching_sessions([own, prior], None)
        self.assertEqual([m.raw_id for m, _, _ in found], ['prior'])

    def test_old_match_beyond_previous_search_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for i in range(305):
                file = Path(directory) / str(i)
                file.write_text('needle' if i == 304 else 'other')
                meta = self.meta(str(i))
                meta.path = file
                files.append(meta)
            with patch.dict(os.environ, {}, clear=True), patch.object(p, 'parse_session', side_effect=lambda meta, **kw: ([p.Turn(1, '', meta.path.read_text())], [])):
                found = p.find_matching_sessions(files, 'needle')
            self.assertEqual(found[0][0].raw_id, '304')

    def test_seconds_and_milliseconds_dates_agree(self):
        self.assertEqual(p.fmt_ts(1794800000), p.fmt_ts(1794800000000))
        self.assertEqual(p.fmt_ts(10**30), "")

    def test_ambiguous_short_id_rejected(self):
        with self.assertRaises(ValueError):
            p.resolve_by_id([self.meta('abcd1'), self.meta('abcd2')], 'codex:abcd')

    def test_explicit_full_id_resolves(self):
        target = self.meta('abcd1')
        self.assertIs(p.resolve_by_id([target, self.meta('abcd2')], 'abcd1'), target)


class HistoryRegressionTests(unittest.TestCase):
    def write(self, directory, agent, rows, name='history.jsonl'):
        path = Path(directory) / name
        path.write_text('\n'.join(json.dumps(row, ensure_ascii=True) for row in rows))
        return p.SessionMeta(agent + ':test', agent, 'test', path, 1)

    def codex_message(self, role, text, **extra):
        return dict(type='response_item', payload=dict(type='message', role=role,
                    content=[dict(type='input_text' if role == 'user' else 'output_text', text=text)]), **extra)

    def test_search_finds_escaped_chinese_and_rejects_unrelated_fields(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            meta = self.write(directory, 'codex', [self.codex_message('user', '中文关键词', unrelated='phantomword')])
            self.assertEqual(len(p.find_matching_sessions([meta], '中文关键词')), 1)
            self.assertEqual(p.find_matching_sessions([meta], 'phantomword'), [])

    def test_unreadable_session_does_not_hide_other_matches(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            good = self.write(directory, 'codex', [self.codex_message('user', 'needle')])
            missing = p.SessionMeta('codex:missing', 'codex', 'missing', Path(directory) / 'gone.jsonl', 2)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                matches = p.find_matching_sessions([missing, good], 'needle')
            self.assertEqual([meta.raw_id for meta, _, _ in matches], ['test'])
            self.assertIn('codex:missing', errors.getvalue())

    def test_search_accepts_words_across_metadata_and_dialogue(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            meta = self.write(directory, 'codex', [self.codex_message('user', 'needle')])
            self.assertEqual(len(p.find_matching_sessions([meta], 'codex needle')), 1)

    def test_antigravity_search_and_read_share_full_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            rows = [dict(type='USER_INPUT', content='original')]
            meta = self.write(directory, 'antigravity', rows, 'transcript.jsonl')
            self.write(directory, 'antigravity', rows + [dict(type='USER_INPUT', content='full-only needle')], 'transcript_full.jsonl')
            matches = p.find_matching_sessions([meta], 'needle')
            self.assertEqual(len(matches), 1)
            self.assertEqual(len(matches[0][1]), len(p.parse_session(meta, use_full=True)[0]))

    def test_grep_shows_context_around_late_assistant_hit(self):
        meta = p.SessionMeta('codex:test', 'codex', 'test', Path('/unused'), 1)
        turn = p.Turn(1, '', 'request', assistant_texts=['x' * 1300 + 'before needle after'])
        result = p.render_read_turns(meta, [turn], None, False, 'needle', False, 60000)
        self.assertIn('before needle after', result)

    def test_pi_tool_result_is_searchable_and_visible(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            rows = [dict(type='message', message=dict(role='user', content=[dict(type='text', text='request')])),
                    dict(type='message', message=dict(role='toolResult', isError=True,
                         content=[dict(type='text', text='build needle failed')]))]
            meta = self.write(directory, 'pi', rows)
            turns, _ = p.parse_session(meta)
            self.assertEqual(turns[0].tool_outputs, ['build needle failed'])
            self.assertEqual(turns[0].errors, ['build needle failed'])
            self.assertEqual(len(p.find_matching_sessions([meta], 'needle')), 1)
            result = p.render_read_turns(meta, turns, None, True, None, True, 60000)
            self.assertIn('build needle failed', result)


if __name__ == '__main__':
    unittest.main()
