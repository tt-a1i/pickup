import contextlib
import importlib.util
import io
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
            with patch.dict(os.environ, {}, clear=True), patch.object(p, 'parse_session', return_value=([p.Turn(1, '', 'needle')], [])):
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


if __name__ == '__main__':
    unittest.main()
