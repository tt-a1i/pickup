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

    def test_pi_candidates_have_unique_resolvable_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / '.pi/agent/sessions/project'
            root.mkdir(parents=True)
            for suffix in ('1', '2'):
                sid = '12345678-0000-0000-0000-00000000000' + suffix
                (root / ('2026-11-16_' + sid + '.jsonl')).write_text(
                    json.dumps(dict(type='session', id=sid, cwd='/example')))
            with patch.object(p, 'HOME', home):
                sessions = p.discover_pi()
            self.assertEqual(len({meta.uid for meta in sessions}), 2)
            for meta in sessions:
                self.assertIs(p.resolve_by_id(sessions, meta.uid), meta)

    def test_pi_discovery_uses_header_id_for_custom_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / '.pi/agent/sessions/project'
            root.mkdir(parents=True)
            (root / 'custom.jsonl').write_text(json.dumps(dict(type='session', id='header-session-id')))
            with patch.object(p, 'HOME', home):
                sessions = p.discover_pi()
            self.assertEqual(sessions[0].raw_id, 'header-session-id')
            self.assertEqual(sessions[0].uid, 'pi:header-session-id')

    def test_claude_and_qoder_text_block_tool_results(self):
        for agent in ('claude', 'qoder'):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
                rows = [dict(type='user', message=dict(content='request')),
                        dict(type='user', message=dict(content=[dict(type='tool_result', content='string output')])),
                        dict(type='user', message=dict(content=[dict(type='tool_result', content=[
                            dict(type='text', text='array needle'),
                            dict(type='image', source=dict(data='image-only')),
                            dict(type='text', text='second block')])]))]
                meta = self.write(directory, agent, rows)
                turns, _ = p.parse_session(meta)
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0].tool_outputs, ['string output', 'array needle\nsecond block'])
                self.assertEqual(len(p.find_matching_sessions([meta], 'needle')), 1)
                self.assertEqual(p.find_matching_sessions([meta], 'image-only'), [])
                result = p.render_read_turns(meta, turns, None, True, None, True, 60000)
                self.assertIn('array needle', result)

    def test_pi_summary_only_history_can_be_found_and_opened(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            meta = self.write(directory, 'pi', [dict(type='session', id='pi-session'),
                dict(type='compaction', id='a', parentId=None, summary='summary needle')])
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(len(p.find_matching_sessions([meta], 'needle')), 1)
                self.assertEqual(len(p.find_matching_sessions([meta], None)), 1)
                turns, notes = p.parse_session(meta)
            self.assertEqual(errors.getvalue(), '')
            self.assertEqual(turns, [])
            overview = p.render_session_overview(meta, turns, notes)
            self.assertIn('summary needle', overview)
            grep = p.render_read_turns(meta, turns, None, False, 'needle', False, 60000, notes=notes)
            self.assertIn('summary needle', grep)

    def test_pi_search_and_read_only_active_branch(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            def message(node_id, parent, role, text):
                return dict(type='message', id=node_id, parentId=parent,
                            message=dict(role=role, content=[dict(type='text', text=text)]))
            rows = [dict(type='session', id='pi-session'),
                    message('a', None, 'user', 'request'),
                    message('b', 'a', 'assistant', 'abandoned-answer'),
                    dict(type='compaction', id='c', parentId='a', summary='prior context'),
                    message('d', 'c', 'assistant', 'active-answer')]
            meta = self.write(directory, 'pi', rows)
            turns, notes = p.parse_session(meta)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0].assistant_texts, ['active-answer'])
            self.assertIn('prior context', notes[0])
            self.assertEqual(len(p.find_matching_sessions([meta], 'prior context')), 1)
            excerpt = p.render_read_turns(meta, turns, None, False, 'prior context', False, 60000, notes=notes)
            self.assertIn('prior context', excerpt)
            self.assertNotIn('No dialogue', excerpt)
            self.assertEqual(excerpt.count('**Session Note**'), 1)
            self.assertEqual(p.find_matching_sessions([meta], 'abandoned-answer'), [])
            self.assertEqual(len(p.find_matching_sessions([meta], 'active-answer')), 1)
            shown = p.render_read_turns(meta, turns, None, True, None, False, 60000)
            self.assertIn('active-answer', shown)
            self.assertNotIn('abandoned-answer', shown)

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
