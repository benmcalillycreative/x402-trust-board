import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from x402trust import config, notes


class ManagedNotes(unittest.TestCase):
    def setUp(self):
        self.saved = config.DATA_DIR
        config.DATA_DIR = Path(tempfile.mkdtemp())
        self.managed = Path(tempfile.mkdtemp()) / "owner-notes.json"
        self.managed.write_text(json.dumps([
            {"match": "a.example", "status": "intended", "note": "from repo"},
            {"match": "b.example", "status": "retired", "note": "from repo"},
            {"match": "bad.example", "status": "nonsense"},
        ]))
        self.patch = mock.patch.object(notes, "managed_path", return_value=self.managed)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        config.DATA_DIR = self.saved

    def test_repo_notes_are_used_and_local_ones_win(self):
        notes.add("a.example", "retired", "said locally")
        got = {n["match"]: n for n in notes.load()}
        self.assertEqual(got["a.example"]["status"], "retired")
        self.assertEqual(got["b.example"]["note"], "from repo")
        self.assertNotIn("bad.example", got)
        self.assertEqual(notes.find(notes.load(), "https://b.example/x")["status"], "retired")

    def test_adding_a_note_never_copies_repo_notes_into_the_local_file(self):
        notes.add("c.example", "intended", "local")
        self.assertEqual([n["match"] for n in notes.load_local()], ["c.example"])
        self.assertEqual(notes.remove("b.example"), 0, "repo notes can't be removed locally")


if __name__ == "__main__":
    unittest.main()
