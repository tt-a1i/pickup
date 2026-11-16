import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pickup_pi import select_entries


def node(node_id, parent, kind='message', **fields):
    return {'type': kind, 'id': node_id, 'parentId': parent, **fields}


class PiContextTest(unittest.TestCase):
    def test_last_persisted_leaf_selects_only_its_ancestors(self):
        header = {'type': 'session', 'id': 'full-session-id'}
        a, b, c, d = node('a', None), node('b', 'a'), node('c', 'b'), node('d', 'a')
        selected, notes = select_entries([header, a, b, c, d])
        self.assertEqual(selected, [header, a, d])
        self.assertEqual(notes, [])

    def test_latest_compaction_and_branch_summary_are_preserved(self):
        a = node('a', None)
        b = node('b', 'a')
        c = node('c', 'b', 'compaction', summary='older context', firstKeptEntryId='b')
        d = node('d', 'c', 'branch_summary', summary='other branch')
        e = node('e', 'd')
        selected, notes = select_entries([a, b, c, d, e])
        self.assertEqual(selected, [a, b, c, d, e])
        self.assertEqual(notes, ['Pi compaction summary: older context', 'Pi branch summary: other branch'])

    def test_legacy_linear_compaction_keeps_earlier_history(self):
        header = {'type': 'session', 'version': 1}
        a, b = {'type': 'message'}, {'type': 'message'}
        c = {'type': 'compaction', 'summary': 'earlier', 'firstKeptEntryIndex': 2}
        self.assertEqual(select_entries([header, a, b, c])[0], [header, a, b, c])

    def test_invalid_tree_never_combines_branches(self):
        for records in ([node('a', None), node('b', 'missing')],
                        [node('a', 'b'), node('b', 'a')],
                        [node('a', None), node('a', None)],
                        [node('a', None), {'type': 'message'}]):
            selected, notes = select_entries(records)
            self.assertEqual(selected, [])
            self.assertTrue(notes)

    def test_legacy_records_are_linear(self):
        records = [{'type': 'session'}, {'type': 'message'}, {'type': 'message'}]
        self.assertEqual(select_entries(records), (records, []))


if __name__ == '__main__':
    unittest.main()
