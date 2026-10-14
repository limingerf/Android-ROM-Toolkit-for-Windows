"""Case-insensitive filesystem collisions.

Android filesystems are case sensitive, so a partition can contain both
``ES-Vodafone`` and ``es-vodafone``.  Windows cannot store both names in one
directory: the extraction merges them, ``shutil.copytree`` aborts with a bare
``WinError 183``, and a tolerant copy silently drops one tree.  These tests
cover the detection that turns the failure into an actionable message, and the
replacement copier that never depends on ``copytree``'s mkdir behaviour.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Primary.Utils import (
    CASE_COLLISION_REMEDY,
    LayoutError,
    copy_tree,
    describe_case_collisions,
    find_case_collisions,
    is_directory_junction,
    raise_on_case_collisions,
)


def make_collision(root, upper="ES-Vodafone", lower="es-vodafone"):
    """Create two directories whose names differ only in case.

    Returns True when the filesystem accepted both names.  NTFS accepts them
    through its POSIX namespace, which is how the extraction produced the
    colliding workspace in the first place; a filesystem that refuses the
    second name cannot be tested this way.
    """
    root = Path(root)
    (root / upper).mkdir(parents=True, exist_ok=True)
    try:
        (root / lower).mkdir()
    except (FileExistsError, OSError):
        return False
    names = {entry.name for entry in os.scandir(root)}
    return upper in names and lower in names


class FindCaseCollisionsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="art-case-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_reports_nothing_for_a_clean_tree(self):
        (self.root / "system" / "etc").mkdir(parents=True)
        (self.root / "system" / "etc" / "Vodafone").write_text("x", encoding="utf-8")
        (self.root / "system" / "bin").mkdir()
        self.assertEqual(find_case_collisions(self.root), [])

    def test_reports_nothing_for_names_that_only_differ_elsewhere(self):
        # Same spelling in two different directories is not a collision.
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        (self.root / "a" / "Alpha").mkdir()
        (self.root / "b" / "Alpha").mkdir()
        self.assertEqual(find_case_collisions(self.root), [])

    def test_detects_a_directory_collision(self):
        if not make_collision(self.root):
            self.skipTest("this filesystem cannot store two case-variant names")
        collisions = find_case_collisions(self.root)
        self.assertEqual(len(collisions), 1)
        directory, kind, names = collisions[0]
        self.assertEqual(kind, "dir")
        self.assertEqual(sorted(names), ["ES-Vodafone", "es-vodafone"])
        self.assertEqual(Path(directory).resolve(), self.root.resolve())

    def test_detects_a_directory_collision_from_the_walk_result(self):
        # Deterministic on every filesystem: a filesystem that merges the two
        # names (plain NTFS for files) cannot produce the collision at all, so
        # the decision logic is exercised on the walk output directly.
        collisions = find_case_collisions(self.root)
        self.assertEqual(collisions, [])
        walk_result = [(str(self.root), ["ES-Vodafone", "es-vodafone"], ["Readme.txt"])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            collisions = find_case_collisions(self.root)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0][1], "dir")
        self.assertEqual(collisions[0][2], ["ES-Vodafone", "es-vodafone"])

    def test_detects_a_file_collision_from_the_walk_result(self):
        walk_result = [(str(self.root), [], ["Readme.txt", "readme.txt"])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            collisions = find_case_collisions(self.root)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0][1], "file")
        self.assertEqual(collisions[0][2], ["Readme.txt", "readme.txt"])

    def test_ignores_names_that_do_not_collide(self):
        walk_result = [(str(self.root), ["bin", "etc"], ["build.prop", "init.rc"])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            self.assertEqual(find_case_collisions(self.root), [])

    def test_detects_a_file_collision(self):
        (self.root / "Readme.txt").write_text("a", encoding="utf-8")
        try:
            (self.root / "readme.txt").write_text("b", encoding="utf-8")
        except (FileExistsError, OSError):
            self.skipTest("this filesystem cannot store two case-variant names")
        if len(list(os.scandir(self.root))) < 2:
            self.skipTest("this filesystem merged the two names")
        collisions = find_case_collisions(self.root)
        self.assertEqual([item[1] for item in collisions], ["file"])

    def test_reports_the_nested_directory_holding_the_collision(self):
        nested = self.root / "my_product" / "cust" / "EUEX" / "etc" / "extension"
        nested.mkdir(parents=True)
        if not make_collision(nested):
            self.skipTest("this filesystem cannot store two case-variant names")
        collisions = find_case_collisions(self.root)
        self.assertEqual(len(collisions), 1)
        self.assertEqual(Path(collisions[0][0]).resolve(), nested.resolve())

    def test_limit_stops_the_scan_early(self):
        walk_result = [
            (str(self.root / "one"), ["Alpha", "alpha"], []),
            (str(self.root / "two"), ["Beta", "beta"], []),
        ]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            self.assertEqual(len(find_case_collisions(self.root, limit=1)), 1)

    def test_directory_symlinks_are_not_followed(self):
        target = self.root / "target"
        target.mkdir()
        if not make_collision(target):
            self.skipTest("this filesystem cannot store two case-variant names")
        link = self.root / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating symbolic links is not permitted here")
        # The collision lives in the link target only; the scan must not report
        # it twice or loop through the link.
        collisions = find_case_collisions(self.root)
        self.assertEqual(len(collisions), 1)


class DescribeCaseCollisionsTests(unittest.TestCase):
    def test_lists_directory_kind_and_spellings(self):
        text = describe_case_collisions(
            [(r"C:\ws\system\etc", "dir", ["ES-Vodafone", "es-vodafone"])]
        )
        self.assertIn(r"C:\ws\system\etc", text)
        self.assertIn("dir", text)
        self.assertIn("ES-Vodafone / es-vodafone", text)

    def test_trims_a_long_list_and_counts_the_rest(self):
        collisions = [(f"/ws/dir{index}", "dir", ["A", "a"]) for index in range(8)]
        text = describe_case_collisions(collisions, limit=3)
        self.assertIn("另有 5 处", text)
        self.assertNotIn("/ws/dir3", text)

    def test_makes_paths_relative_to_the_root(self):
        text = describe_case_collisions(
            [("/ws/system/etc", "dir", ["A", "a"])], root="/ws"
        )
        self.assertIn(os.path.join("/ws", "system", "etc"), text)

    def test_root_itself_is_shown_without_a_dot_segment(self):
        text = describe_case_collisions([("/ws", "dir", ["A", "a"])], root="/ws")
        self.assertNotIn(os.curdir + " (", text)
        self.assertIn("/ws (dir: A / a)", text)


class RaiseOnCaseCollisionsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="art-case-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_returns_none_on_a_clean_tree(self):
        (self.root / "etc").mkdir()
        self.assertIsNone(raise_on_case_collisions(self.root))

    def test_skips_the_check_off_windows(self):
        # A POSIX host keeps both names, so there is nothing to refuse.
        with mock.patch("Scripts.Primary.Utils.os.name", "posix"):
            with mock.patch(
                "Scripts.Primary.Utils.find_case_collisions"
            ) as finder:
                self.assertIsNone(raise_on_case_collisions(self.root))
        finder.assert_not_called()

    def test_raises_with_the_directory_and_the_remedy(self):
        collisions = [(str(self.root / "etc"), "dir", ["ES-Vodafone", "es-vodafone"])]
        with mock.patch("Scripts.Primary.Utils.os.name", "nt"):
            with mock.patch(
                "Scripts.Primary.Utils.find_case_collisions", return_value=collisions
            ):
                with self.assertRaises(LayoutError) as caught:
                    raise_on_case_collisions(self.root)
        message = str(caught.exception)
        self.assertIn("ES-Vodafone / es-vodafone", message)
        self.assertIn("etc", message)
        self.assertIn(CASE_COLLISION_REMEDY, message)


class CopyTreeTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(tempfile.mkdtemp(prefix="art-src-"))
        self.destination = Path(tempfile.mkdtemp(prefix="art-dst-")) / "system"
        self.addCleanup(shutil.rmtree, self.source, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.destination.parent, ignore_errors=True)

    def test_copies_files_and_nested_directories(self):
        (self.source / "etc" / "init").mkdir(parents=True)
        (self.source / "etc" / "init" / "rc").write_text("data", encoding="utf-8")
        (self.source / "build.prop").write_text("prop", encoding="utf-8")
        copy_tree(self.source, self.destination)
        self.assertEqual(
            (self.destination / "etc" / "init" / "rc").read_text(encoding="utf-8"),
            "data",
        )
        self.assertEqual(
            (self.destination / "build.prop").read_text(encoding="utf-8"), "prop"
        )

    def test_keeps_an_existing_destination_directory(self):
        self.destination.mkdir(parents=True)
        (self.destination / "keep").write_text("kept", encoding="utf-8")
        (self.source / "new").write_text("new", encoding="utf-8")
        copy_tree(self.source, self.destination)
        self.assertTrue((self.destination / "keep").is_file())
        self.assertTrue((self.destination / "new").is_file())

    def test_preserves_symbolic_links(self):
        (self.source / "bin").mkdir()
        (self.source / "bin" / "tool").write_text("x", encoding="utf-8")
        try:
            os.symlink("tool", self.source / "bin" / "sh")
        except (OSError, NotImplementedError):
            self.skipTest("creating symbolic links is not permitted here")
        copy_tree(self.source, self.destination)
        link = self.destination / "bin" / "sh"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "tool")

    def test_recreates_a_file_link_from_the_link_target(self):
        # Deterministic on a host without the symbolic-link privilege: the walk
        # output is mocked so the recreation call can be asserted directly.
        (self.source / "bin").mkdir()
        (self.source / "bin" / "tool").write_text("x", encoding="utf-8")
        walk_result = [(str(self.source), [], ["sh"])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            with mock.patch("Scripts.Primary.Utils.os.path.islink", return_value=True):
                with mock.patch(
                    "Scripts.Primary.Utils.os.path.isdir", return_value=False
                ):
                    with mock.patch(
                        "Scripts.Primary.Utils.os.readlink", return_value="/system/bin/tool"
                    ):
                        with mock.patch("Scripts.Primary.Utils.os.symlink") as link:
                            copy_tree(self.source, self.destination)
        link.assert_called_once()
        target, created = link.call_args.args[:2]
        self.assertEqual(target, "/system/bin/tool")
        self.assertEqual(Path(created).name, "sh")
        self.assertFalse(link.call_args.kwargs["target_is_directory"])

    def test_recreates_a_directory_link_as_a_directory_link(self):
        (self.source / "real").mkdir()
        walk_result = [(str(self.source), ["link"], [])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            with mock.patch("Scripts.Primary.Utils.os.path.islink", return_value=True):
                with mock.patch(
                    "Scripts.Primary.Utils.os.path.isdir", return_value=True
                ):
                    with mock.patch(
                        "Scripts.Primary.Utils.os.readlink", return_value="real"
                    ):
                        with mock.patch("Scripts.Primary.Utils.os.symlink") as link:
                            copy_tree(self.source, self.destination)
        link.assert_called_once()
        self.assertTrue(link.call_args.kwargs["target_is_directory"])

    def test_merges_a_case_collision_without_raising(self):
        # The pre-copy audit refuses this tree; if the copy is reached anyway it
        # must not abort with WinError 183 half way through.
        (self.source / "extension" / "ES-Vodafone").mkdir(parents=True)
        (self.source / "extension" / "ES-Vodafone" / "a.txt").write_text(
            "upper", encoding="utf-8"
        )
        try:
            (self.source / "extension" / "es-vodafone").mkdir()
        except (FileExistsError, OSError):
            self.skipTest("this filesystem cannot store two case-variant names")
        (self.source / "extension" / "es-vodafone" / "b.txt").write_text(
            "lower", encoding="utf-8"
        )
        (self.source / "after.txt").write_text("later", encoding="utf-8")
        copy_tree(self.source, self.destination)
        # The copy completes and every later entry still lands.
        self.assertTrue((self.destination / "after.txt").is_file())

    def test_merges_a_collision_reported_by_the_walk(self):
        # Same guarantee, exercised without depending on the filesystem keeping
        # both spellings: the second directory already exists at the target.
        (self.source / "extension").mkdir()
        (self.source / "extension" / "a.txt").write_text("upper", encoding="utf-8")
        walk_result = [
            (str(self.source), ["extension"], []),
            (str(self.source / "extension"), ["ES-Vodafone", "es-vodafone"], ["a.txt"]),
        ]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            copy_tree(self.source, self.destination)
        self.assertTrue((self.destination / "extension" / "a.txt").is_file())

    def test_reports_an_unreadable_source_file(self):
        (self.source / "locked").write_text("x", encoding="utf-8")
        with mock.patch(
            "Scripts.Primary.Utils.shutil.copy2",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(shutil.Error) as caught:
                copy_tree(self.source, self.destination)
        self.assertIn("denied", str(caught.exception))

    def test_symlink_failure_falls_back_to_a_regular_copy(self):
        (self.source / "file").write_text("payload", encoding="utf-8")
        try:
            os.symlink("file", self.source / "link")
        except (OSError, NotImplementedError):
            self.skipTest("creating symbolic links is not permitted here")
        with mock.patch(
            "Scripts.Primary.Utils.os.symlink", side_effect=OSError("no privilege")
        ):
            copy_tree(self.source, self.destination)
        copied = self.destination / "link"
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), "payload")

    def test_existing_destination_link_is_not_replaced(self):
        (self.source / "file").write_text("payload", encoding="utf-8")
        walk_result = [(str(self.source), [], ["link"])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            with mock.patch("Scripts.Primary.Utils.os.path.islink", return_value=True):
                with mock.patch("Scripts.Primary.Utils.os.readlink", return_value="x"):
                    with mock.patch(
                        "Scripts.Primary.Utils.os.symlink",
                        side_effect=FileExistsError("exists"),
                    ) as link:
                        copy_tree(self.source, self.destination)
        link.assert_called_once()

    def test_skips_a_directory_junction(self):
        # os.walk descends into a junction even with followlinks=False, so the
        # copy must recognise and skip it instead of duplicating its tree.
        walk_result = [(str(self.source), ["junction"], [])]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            with mock.patch(
                "Scripts.Primary.Utils.is_directory_junction", return_value=True
            ):
                copy_tree(self.source, self.destination)
        self.assertFalse((self.destination / "junction").exists())

    def test_copies_an_ordinary_directory_next_to_a_junction(self):
        (self.source / "ordinary").mkdir()
        (self.source / "ordinary" / "file").write_text("x", encoding="utf-8")
        walk_result = [
            (str(self.source), ["ordinary", "junction"], []),
            (str(self.source / "ordinary"), [], ["file"]),
        ]
        with mock.patch("Scripts.Primary.Utils.os.walk", return_value=walk_result):
            with mock.patch(
                "Scripts.Primary.Utils.is_directory_junction",
                side_effect=lambda path: path.endswith("junction"),
            ):
                copy_tree(self.source, self.destination)
        self.assertTrue((self.destination / "ordinary" / "file").is_file())
        self.assertFalse((self.destination / "junction").exists())


class DirectoryJunctionTests(unittest.TestCase):
    def test_is_false_off_windows(self):
        with mock.patch("Scripts.Primary.Utils.os.name", "posix"):
            self.assertFalse(is_directory_junction("anything"))

    def test_is_false_for_a_missing_path(self):
        self.assertFalse(is_directory_junction(r"Z:\definitely\missing\path"))

    def test_is_true_for_a_mount_point_reparse_tag(self):
        import stat as stat_module

        info = mock.Mock(st_reparse_tag=stat_module.IO_REPARSE_TAG_MOUNT_POINT)
        with mock.patch("Scripts.Primary.Utils.os.name", "nt"):
            with mock.patch("Scripts.Primary.Utils.os.lstat", return_value=info):
                self.assertTrue(is_directory_junction(r"C:\link"))

    def test_is_false_for_a_symbolic_link_reparse_tag(self):
        import stat as stat_module

        info = mock.Mock(st_reparse_tag=stat_module.IO_REPARSE_TAG_SYMLINK)
        with mock.patch("Scripts.Primary.Utils.os.name", "nt"):
            with mock.patch("Scripts.Primary.Utils.os.lstat", return_value=info):
                self.assertFalse(is_directory_junction(r"C:\link"))


if __name__ == "__main__":
    unittest.main()
