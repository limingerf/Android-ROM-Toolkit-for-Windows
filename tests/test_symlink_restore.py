"""Regressions for symlink restoration before repacking.

Windows cannot always create symbolic links, so extraction materializes them as
hard links or as placeholder files that contain the link target.  `e2fsdroid`
reports `ignored token` for the link target in the fsconfig and `mkfs.erofs`
reads the inode type from the source tree, so both repackers must restore the
links from the workspace before building the image.  A workspace whose source
image holds 423 symlinks used to repack into an image with none.

fsconfig paths carry the partition name (`system/bin`) while `dir_path` is the
partition directory itself, so an entry maps to `<parent of dir_path>/<entry>`.
"""
import os
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.FileConfigPatcher import restore_symlinks


def symlinks_supported(root: Path) -> bool:
    probe = root / ".art-symlink-probe"
    try:
        os.symlink("target", probe)
    except (OSError, NotImplementedError):
        return False
    os.unlink(probe)
    return True


class RestoreSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "WORKSPACE"
        self.partition = self.workspace / "system"
        self.partition.mkdir(parents=True)
        self.can_link = symlinks_supported(Path(self._tmp.name))

        # A placeholder file, exactly as _materialize_windows_symlink writes it.
        (self.partition / "bin").write_text("/system/bin", encoding="utf-8")
        # A hard link to the resolved target: the other materialized shape.
        real_init = self.partition / "system" / "bin" / "init"
        real_init.parent.mkdir(parents=True)
        real_init.write_text("ELF-ish init payload", encoding="utf-8")
        os.link(real_init, self.partition / "init")
        # A deliberate user edit (a real directory replacing a symlink).
        (self.partition / "vendor" / "lib64").mkdir(parents=True)
        (self.partition / "vendor" / "lib64" / "libc.so").write_text("elf", encoding="utf-8")

        self.fsconfig = self.workspace / "system_fsconfig.txt"
        self.fsconfig.write_text(
            "/ 0 0 0755\n"
            "system 0 0 0755\n"
            "system/bin 0 0 0644 /system/bin\n"
            "system/init 0 2000 0750 /system/bin/init\n"
            "system/vendor 0 0 0755 /vendor\n"
            "system/plain 0 0 0644\n"
            "system/capped 0 0 0755 capabilities=0x2000\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_capability_and_plain_entries_are_not_links(self):
        report = restore_symlinks(str(self.partition), str(self.fsconfig))
        self.assertEqual(report["declared"], 3)

    def test_materialized_links_are_restored_and_edits_are_kept(self):
        report = restore_symlinks(str(self.partition), str(self.fsconfig))
        self.assertEqual(report["skipped"], ["system/vendor"])
        if self.can_link:
            self.assertEqual(report["restored"], 2)
            self.assertEqual(report["failed"], [])
            self.assertTrue(os.path.islink(self.partition / "bin"))
            self.assertEqual(os.readlink(self.partition / "bin"), "/system/bin")
            self.assertTrue(os.path.islink(self.partition / "init"))
        else:
            self.assertEqual(report["restored"], 0)
            self.assertEqual(len(report["failed"]), 2)
            # A refused link must never delete the materialized file.
            self.assertEqual(
                (self.partition / "bin").read_text(encoding="utf-8"), "/system/bin"
            )
        # The user's own directory replacement is untouched either way.
        self.assertTrue((self.partition / "vendor" / "lib64" / "libc.so").is_file())

    def test_already_correct_links_are_reported_as_present(self):
        if not self.can_link:
            self.skipTest("symbolic links are not permitted here")
        placeholder = self.partition / "bin"
        placeholder.unlink()
        os.symlink("/system/bin", placeholder)
        report = restore_symlinks(str(self.partition), str(self.fsconfig))
        self.assertEqual(report["present"], 1)
        self.assertEqual(report["restored"], 1)

    def test_no_staging_files_are_left_behind(self):
        restore_symlinks(str(self.partition), str(self.fsconfig))
        leftovers = [p.name for p in self.workspace.rglob("*.art-link-staging")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
