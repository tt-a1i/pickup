import base64
import json
from pathlib import Path
import re
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pickup_paging as paging


class PagingTests(unittest.TestCase):
    def page(self, text, **kwargs):
        args = dict(session_id='codex:full-session-id', fingerprint='decoded-sha256',
                    operation='read', options={}, max_chars=1400)
        args.update(kwargs)
        return paging.render_page(text, **args)

    def unpack(self, page):
        match = re.match(r'# Pickup page: characters (\d+):(\d+)/(\d+)\n\n', page)
        self.assertIsNotNone(match)
        start, end, total = map(int, match.groups())
        payload = page[match.end():match.end() + end - start]
        next_lines = [line for line in page.splitlines() if line.startswith('Next: ')]
        token = shlex.split(next_lines[-1][6:])[-1] if next_lines else None
        return start, end, total, payload, token

    def test_huge_first_turn_unicode_and_whitespace_reconstruct_exactly(self):
        text = '### Turn 1\n' + ('漢字🙂 e\u0301 ' * 1500) + '\n\n  final spaces  \n'
        chunks, cursor, offset = [], None, 0
        for index in range(100):
            budget = 1400 if index % 2 else 1800
            page = self.page(text, cursor=cursor, max_chars=budget)
            self.assertLessEqual(len(page) + 1, budget)
            self.assertFalse(page.endswith('\n'))
            start, end, total, payload, cursor = self.unpack(page)
            self.assertEqual(start, offset)
            self.assertGreater(end, start)
            self.assertEqual(total, len(text))
            chunks.append(payload)
            offset = end
            if cursor is None:
                self.assertIn('complete=yes', page)
                break
            self.assertIn('complete=no', page)
        else:
            self.fail('pagination failed to finish')
        self.assertEqual(''.join(chunks), text)

    def test_every_view_uses_same_bound_and_continuation(self):
        for operation, options, view in [('show', {}, 'overview'),
                                         ('read', {}, 'dialogue'),
                                         ('read', {'grep': 'needle'}, 'grep')]:
            with self.subTest(view=view):
                page = self.page(('notes and outlines and grep hits\n' * 1000),
                                 operation=operation, options=options)
                self.assertLessEqual(len(page) + 1, 1400)
                self.assertIn(f'view={view} complete=no', page)
                self.assertIsNotNone(self.unpack(page)[4])

    def test_default_and_custom_byte_budgets_include_footer_and_newline(self):
        text = ('界🙂' * 3000) + '\nlast line'
        for max_chars, max_bytes, expected in [(None, None, 8000), (None, 1250, 1250), (3000, 1800, 1800)]:
            with self.subTest(max_chars=max_chars, max_bytes=max_bytes):
                cursor, chunks = None, []
                while True:
                    page = self.page(text, max_chars=max_chars, max_bytes=max_bytes, cursor=cursor)
                    self.assertLessEqual(len((page + '\n').encode('utf-8')), expected)
                    start, end, _, payload, cursor = self.unpack(page)
                    self.assertGreater(end, start)
                    chunks.append(payload)
                    if cursor is None:
                        self.assertTrue(page.endswith('End of selected view.'))
                        self.assertIn('complete=yes', page)
                        break
                    self.assertIn('complete=no', page)
                    self.assertLess(len(chunks), 100)
                    if max_bytes is not None:
                        self.assertIn(f'--max-bytes {max_bytes} --cursor', page)
                self.assertEqual(''.join(chunks), text)
        char_only = self.page(text, max_chars=3000)
        self.assertGreater(len(char_only.encode('utf-8')), 8000)
        self.assertLessEqual(len(char_only) + 1, 3000)

    def test_cursor_recovers_options_and_quotes_command_arguments(self):
        options = {'grep': 'quoted "needle" 中文', 'with_tools': True, 'turns': None}
        page = self.page('x' * 5000, options=options,
                         command_prefix=['/a path/python3', '/skill path/pickup.py'])
        token = self.unpack(page)[4]
        data = paging.decode_cursor(token)
        self.assertEqual(data['options'], options)
        command = shlex.split(page.split('Next: ')[-1])
        self.assertEqual(command, ['/a path/python3', '/skill path/pickup.py',
                                   'read', 'codex:full-session-id', '--max-chars', '1400', '--cursor', token])

    def test_changed_source_session_or_options_fail(self):
        token = self.unpack(self.page('x' * 5000))[4]
        for changes, message in [({'fingerprint': 'appended-turn'}, 'Session changed'),
                                 ({'session_id': 'codex:other'}, 'another session'),
                                 ({'options': {'grep': 'x'}}, 'view options'),
                                 ({'operation': 'show'}, 'view options')]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.page('x' * 5000, cursor=token, **changes)

    def encode(self, data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')

    def test_malformed_and_strict_field_validation(self):
        data = paging.decode_cursor(self.unpack(self.page('x' * 5000))[4])
        tokens = ['', '!!', 'eA', self.encode([]), self.encode({})]
        for key, value in [('version', True), ('version', 2), ('session_id', ''),
                           ('fingerprint', None), ('operation', 'export'),
                           ('options', []), ('offset', True), ('offset', -1),
                           ('offset', 1.5)]:
            changed = dict(data, **{key: value})
            tokens.append(self.encode(changed))
        tokens.append(self.encode(dict(data, extra='field')))
        tokens.append(self.encode(dict(data, options={'value': float('nan')})))
        for token in tokens:
            with self.subTest(token=token), self.assertRaises(ValueError):
                paging.decode_cursor(token)

    def test_out_of_range_cursor_fails(self):
        data = paging.decode_cursor(self.unpack(self.page('x' * 5000))[4])
        for offset in (5000, 5001):
            with self.assertRaisesRegex(ValueError, 'outside'):
                self.page('x' * 5000, cursor=self.encode(dict(data, offset=offset)))

    def test_tiny_budgets_and_oversized_metadata_fail_without_output(self):
        for budget in (1, 1023, True):
            with self.assertRaises(ValueError):
                self.page('x', max_chars=budget)
        with self.assertRaisesRegex(ValueError, 'cannot fit'):
            self.page('x' * 5000, options={'grep': 'x' * 2000}, max_chars=1024)

    def test_empty_and_small_views_finish(self):
        for text in ('', 'small\n', '  spaced  '):
            page = self.page(text)
            self.assertEqual(self.unpack(page)[3], text)
            self.assertIsNone(self.unpack(page)[4])
            self.assertIn('complete=yes', page)

    def test_complete_footer_can_fit_when_continuation_footer_cannot(self):
        page = self.page('x' * 800, max_chars=1024, options={'grep': 'x' * 1000})
        self.assertLessEqual(len(page) + 1, 1024)
        self.assertIn('complete=yes', page)


if __name__ == '__main__':
    unittest.main()
