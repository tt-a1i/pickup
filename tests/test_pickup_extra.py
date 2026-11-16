import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pickup_extra as extra


class ReadersTest(unittest.TestCase):
    def test_kimi_uses_committed_messages_not_replayed_events(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / 'wd_project' / 'session_abc'
            main = session / 'agents' / 'main'
            main.mkdir(parents=True)
            (session / 'state.json').write_text(json.dumps({'id': 'abc', 'cwd': '/project', 'title': 'test'}))
            def msg(role, content, **fields):
                return {'type': 'agent.message.appended', 'time': 1700000000000, 'message': {'message': {'role': role, 'content': [{'type': 'text', 'text': content}], **fields}}}
            records = [msg('user', 'question'), {'type': 'context.append_loop_event', 'event': {'type': 'content.part', 'part': {'type': 'text', 'text': 'duplicate'}}}, msg('assistant', 'answer', toolCalls=[{'name': 'Shell', 'arguments': '{"cmd":"ls"}'}]), msg('tool', 'result')]
            (main / 'wire.jsonl').write_text('\n'.join(map(json.dumps, records)))
            self.assertEqual(extra.discover_kimi(temp)[0]['raw_id'], 'abc')
            turns = extra.parse_kimi(session)
            self.assertEqual(turns[0]['assistant_texts'], ['answer'])
            self.assertEqual(turns[0]['tool_outputs'], ['result'])
            self.assertTrue(turns[0]['tool_summaries'][0].startswith('Shell('))

    def test_devin_follows_active_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'sessions.db'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE sessions (id TEXT, working_directory TEXT, title TEXT, last_activity_at INTEGER, hidden INTEGER, main_chain_id INTEGER); CREATE TABLE message_nodes (session_id TEXT, node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER);')
                c.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)', ('s', '/project', 'test', 1700000000, 0, 3))
                for node, parent, message in [(1, None, {'role': 'user', 'content': 'question', 'metadata': {'is_user_input': True}}), (2, 1, {'role': 'assistant', 'content': 'abandoned'}), (3, 1, {'role': 'assistant', 'content': 'active'})]:
                    c.execute('INSERT INTO message_nodes VALUES (?, ?, ?, ?, ?)', ('s', node, parent, json.dumps(message), 1700000000))
            item = extra.discover_devin(db)[0]
            self.assertEqual(extra.parse_devin(item['path'])[0]['assistant_texts'], ['active'])

if __name__ == '__main__':
    unittest.main()
