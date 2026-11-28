import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pickup_scan import QueryPrefilter, ScanReport
import pickup_extra


class QueryPrefilterTest(unittest.TestCase):
    def test_case_escapes_and_blocks(self):
        cases = [
            ('本地会话', '本地会话', False, True),
            ('本地会话', '本地会话', True, True),
            ('本地会话', r'\u672c\u5730\u4f1a\u8bdd', False, False),
            ('interrupted session', 'SESSION\nwas INTERRUPTED', False, True),
            ('hello "world"\\path', 'hello "world"\\path', True, True),
            ('kelvin', 'KELVIN', False, True),
            ('ascii', 'ASCİİ', False, True),
            ('some query', 'unrelated Cyrillic Ж', False, False),
        ]
        for query, text, ascii_only, expected in cases:
            with self.subTest(query=query, text=text, ascii_only=ascii_only):
                row = json.dumps({'text': 'x' * (256 * 1024 - 11) + text}, ensure_ascii=ascii_only)
                stream = io.BytesIO((row + '\n{"text":"other row"}\n').encode('utf-8'))
                self.assertEqual(QueryPrefilter(query).may_match(stream), expected)


class ScanReportTest(unittest.TestCase):
    def test_throttling_source_transitions_and_partial_coverage(self):
        output = io.StringIO()
        times = iter([0, .2, .9, 1.1, 1.2])
        report = ScanReport(['kimi', 'devin', 'pi'], stream=output, clock=lambda: next(times))
        report.source('kimi', True)
        report.source('devin', True)
        report.source('pi', False)
        report.discovered('kimi', 5)
        report.discovered('devin', 1)
        report.excluded('kimi')
        report.attempted('kimi', 'one')
        report.readable('kimi')
        report.attempted('kimi', 'two')
        report.failed('kimi', 'two', 'unreadable')
        report.attempted('kimi', 'three')
        report.attempted('kimi', 'four')
        report.attempted('devin', 'five')
        report.readable('devin')
        self.assertEqual(report.finish(), 'partial')
        self.assertEqual(report.stats['kimi']['unscanned'], 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(sum(line.startswith('Scanning kimi:') for line in lines), 2)
        self.assertEqual(sum(line.startswith('Scanning devin:') for line in lines), 1)
        self.assertIn('pi: absent', output.getvalue())
        self.assertIn('failed=1', output.getvalue())

    def test_limit_termination_is_partial_and_absent_is_not_failure(self):
        output = io.StringIO()
        report = ScanReport(['kimi', 'devin'], stream=output, clock=lambda: 0)
        report.source('kimi', True)
        report.source('devin', False)
        report.discovered('kimi', 3)
        report.attempted('kimi', 'one')
        report.readable('kimi')
        self.assertEqual(report.finish(), 'partial')
        self.assertEqual(report.stats['kimi']['unscanned'], 2)
        self.assertIn('discovery_failed=0', output.getvalue())
        report2 = ScanReport(['devin'], stream=io.StringIO())
        report2.source('devin', False)
        self.assertEqual(report2.finish(), 'complete')

    def test_warning_marks_coverage_partial(self):
        report = ScanReport(['kimi'], stream=io.StringIO())
        report.source('kimi', True)
        report.warning('kimi', 'metadata fallback')
        self.assertEqual(report.finish(), 'partial')

    def test_kimi_reports_bad_record_and_keeps_healthy_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            wire = session / 'agents' / 'main' / 'wire.jsonl'
            wire.parent.mkdir(parents=True)
            def message(role, text):
                return {'type': 'agent.message.appended', 'message': {'message': {
                    'role': role, 'content': [{'type': 'text', 'text': text}]}}}
            wire.write_text('\n'.join([json.dumps(message('user', 'hello')), 'invalid', '',
                                       json.dumps({'type': 'unknown'}), json.dumps(message('assistant', 'reply'))]))
            errors = []
            turns = pickup_extra.parse_kimi(session, lambda *x: errors.append(x))
            self.assertEqual(turns[0]['assistant_texts'], ['reply'])
            self.assertEqual(errors, [('kimi', 'Malformed transcript record')])

    def test_devin_reports_corruption_and_broken_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'sessions.db'
            with sqlite3.connect(db) as conn:
                conn.executescript('CREATE TABLE sessions (id TEXT, main_chain_id INTEGER); CREATE TABLE message_nodes (session_id TEXT, node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER);')
                conn.execute('INSERT INTO sessions VALUES (?, ?)', ('s', 3))
                rows = [(1, 99, {'role': 'user', 'content': 'hello', 'metadata': {'is_user_input': True}}),
                        (2, 1, None), (3, 2, {'role': 'assistant', 'content': 'reply'})]
                for node, parent, message in rows:
                    conn.execute('INSERT INTO message_nodes VALUES (?, ?, ?, ?, ?)', ('s', node, parent, json.dumps(message) if message else 'invalid', 1700000000))
            errors = []
            turns = pickup_extra.parse_devin(str(db) + '#s', lambda *x: errors.append(x))
            self.assertEqual(turns[0]['assistant_texts'], ['reply'])
            self.assertEqual({reason for _, reason in errors}, {'Malformed message record', 'Active message chain is missing or cyclic'})
            with sqlite3.connect(db) as conn:
                conn.execute('UPDATE message_nodes SET parent_node_id=3 WHERE node_id=1')
            errors.clear()
            pickup_extra.parse_devin(str(db) + '#s', lambda *x: errors.append(x))
            self.assertIn(('devin', 'Active message chain is missing or cyclic'), errors)

    def test_discovery_errors_are_reported_without_file_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / 'wd_a' / 'session_a'
            session.mkdir(parents=True)
            (session / 'state.json').write_text('private-invalid-json')
            db = root / 'sessions.db'
            db.write_text('private-invalid-database')
            errors = []
            self.assertEqual(pickup_extra.discover_kimi(root, lambda *x: errors.append(x)), [])
            self.assertEqual(pickup_extra.discover_devin(db, lambda *x: errors.append(x)), [])
            self.assertEqual([x[0] for x in errors], ['kimi', 'devin'])
            self.assertIn('Malformed', errors[0][1])
            self.assertNotIn('private', repr(errors))
            errors.clear()
            self.assertEqual(pickup_extra.discover_kimi(root / 'missing', lambda *x: errors.append(x)), [])
            self.assertEqual(pickup_extra.discover_devin(root / 'missing.db', lambda *x: errors.append(x)), [])
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
