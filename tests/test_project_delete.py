"""Regression: an empty DNA_ directory could not be deleted.

``ProjectLayout.state()`` reports "empty" for a project directory that exists
but has no INPUT/OUT/WORKSPACE children (created by hand, or left by a failed
initialisation), and ``delete_project`` rejected that state.  The GUI lists
such projects, so the delete button failed and the directory stayed forever.
"""
import tempfile
import unittest
from pathlib import Path

from Scripts.application import ArtController


class DeleteEmptyProjectTests(unittest.TestCase):
    def test_empty_project_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = ArtController(root)
            project = root / "DNA_empty"
            project.mkdir()
            result = controller.delete_project("DNA_empty")
            self.assertTrue(result["deleted"])
            self.assertEqual(result["preserved"], [])
            self.assertFalse(project.exists())

    def test_unrecognised_root_entry_is_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = ArtController(root)
            project = root / "DNA_empty"
            project.mkdir()
            (project / "random.bin").write_bytes(b"x")
            with self.assertRaises(ValueError):
                controller.delete_project("DNA_empty")
            self.assertTrue(project.exists())


if __name__ == "__main__":
    unittest.main()
