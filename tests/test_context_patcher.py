"""SELinux label completion for paths added to an extracted workspace."""
import tempfile
import unittest
from pathlib import Path

from Scripts.Primary.FileConfigPatcher import (
    CONFIG_SUFFIXES,
    escape_context_path,
    patch_contexts,
    persist_context_rules,
)
from Scripts.ReMake.metadata import normalize_metadata

REPOSITORY = Path(__file__).resolve().parent.parent
CATCH_ALL = "/system(/.*)? u:object_r:rootfs:s0"
PARTITION = "/system u:object_r:rootfs:s0"


class EscapeContractTests(unittest.TestCase):
    """The patcher and the standalone extractor must escape identically."""

    def test_extractor_still_escapes_the_same_characters(self):
        source = (REPOSITORY / "Scripts" / "Extract" / "ext4.py").read_text(encoding="utf-8")
        self.assertIn("for character in '\\\\^$.|?*+(){}[]':", source)

    def test_escape_matches_the_extractor_rule(self):
        self.assertEqual(
            escape_context_path("/system/etc/a.b+c(d)[e].xml"),
            "/system/etc/a\\.b\\+c\\(d\\)\\[e\\]\\.xml",
        )


class ContextPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "system"
        self.workspace.mkdir()
        self.contexts = self.root / "system_contexts.txt"

    def write_rules(self, *rules):
        lines = [PARTITION, CATCH_ALL, *rules]
        self.contexts.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def make(self, relative, content="x"):
        target = self.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def patch(self):
        before = self.contexts.read_text(encoding="utf-8").splitlines()
        count = patch_contexts(str(self.workspace), str(self.contexts))
        after = self.contexts.read_text(encoding="utf-8").splitlines()
        return count, after[len(before):]

    def labelled_etc(self):
        for name in ("tool.bin", "other.so", "keep.rc"):
            self.make(f"etc/init/{name}")
        self.write_rules(
            "/system/etc u:object_r:system_file:s0",
            "/system/etc/init u:object_r:system_file:s0",
            "/system/etc/init/tool\\.bin u:object_r:system_file:s0",
            "/system/etc/init/other\\.so u:object_r:system_file:s0",
            "/system/etc/init/keep\\.rc u:object_r:vendor_init_rc:s0",
        )

    def test_config_file_follows_a_config_sibling(self):
        self.labelled_etc()
        self.make("etc/init/new.rc")
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertEqual(added, ["/system/etc/init/new\\.rc u:object_r:vendor_init_rc:s0"])

    def test_plain_file_follows_the_regular_files_beside_it(self):
        self.labelled_etc()
        self.make("etc/init/new.sh")
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertEqual(added, ["/system/etc/init/new\\.sh u:object_r:system_file:s0"])

    def test_catch_all_does_not_block_an_explicit_line(self):
        self.labelled_etc()
        self.make("etc/init/new.xml")
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertTrue(added[0].startswith("/system/etc/init/new\\.xml u:object_r:"))

    def test_fully_labelled_workspace_is_left_alone(self):
        self.labelled_etc()
        self.assertEqual(self.patch(), (0, []))

    def test_user_regex_rule_is_respected(self):
        self.labelled_etc()
        self.contexts.write_text(
            self.contexts.read_text(encoding="utf-8")
            + "/system/etc/init/.*\\.rc u:object_r:vendor_init_rc:s0\n",
            encoding="utf-8",
        )
        self.make("etc/init/added.rc")
        self.assertEqual(self.patch(), (0, []))

    def test_new_directory_follows_a_sibling_directory(self):
        (self.workspace / "etc" / "init").mkdir(parents=True)
        self.write_rules(
            "/system/etc u:object_r:rootfs:s0",
            "/system/etc/init u:object_r:vendor_file:s0",
        )
        (self.workspace / "etc" / "fresh").mkdir()
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertEqual(added, ["/system/etc/fresh u:object_r:vendor_file:s0"])

    def test_falls_back_to_the_containing_directory_label(self):
        (self.workspace / "empty").mkdir()
        self.write_rules("/system/empty u:object_r:vendor_file:s0")
        self.make("empty/new.txt")
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertEqual(added, ["/system/empty/new\\.txt u:object_r:vendor_file:s0"])

    def test_second_run_adds_nothing(self):
        self.labelled_etc()
        self.make("etc/init/new.rc")
        self.assertEqual(self.patch()[0], 1)
        self.assertEqual(self.patch(), (0, []))

    def test_appended_rule_is_last_and_gets_byte_escaped(self):
        self.labelled_etc()
        self.make("etc/init/新配置.xml")
        count, added = self.patch()
        self.assertEqual(count, 1)
        self.assertEqual(self.contexts.read_text(encoding="utf-8").splitlines()[-1], added[0])
        normalize_metadata(str(self.contexts))
        last = self.contexts.read_text(encoding="utf-8").splitlines()[-1]
        self.assertTrue(last.startswith("/system/etc/init/"))
        self.assertIn("\\346\\226\\260", last)
        self.assertIn("u:object_r:vendor_init_rc:s0", last)

    def test_no_inheritable_label_adds_nothing(self):
        self.make("etc/new.txt")
        self.contexts.write_text("", encoding="utf-8")
        self.assertEqual(self.patch(), (0, []))

    def test_config_suffixes_cover_the_requested_formats(self):
        for suffix in (".prop", ".rc", ".xml"):
            self.assertIn(suffix, CONFIG_SUFFIXES)


class PersistContextRulesTests(unittest.TestCase):
    """Completed rules must reach the project file the user edits."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "system_contexts.txt"
        self.staged = self.root / "staged_contexts.txt"

    def test_appends_only_missing_lines(self):
        self.project.write_bytes(b"\xef\xbb\xbf/system u:object_r:rootfs:s0\r\n")
        self.staged.write_text(
            "/system u:object_r:rootfs:s0\n"
            "/system/etc/new\\.rc u:object_r:system_file:s0\n",
            encoding="utf-8",
        )
        self.assertEqual(persist_context_rules(str(self.project), str(self.staged)), 1)
        text = self.project.read_text(encoding="utf-8")
        self.assertEqual(text.count("/system u:object_r:rootfs:s0"), 1)
        self.assertIn("/system/etc/new\\.rc u:object_r:system_file:s0", text)

    def test_equivalent_escaping_is_not_duplicated(self):
        self.project.write_text("/system/etc/a.b u:object_r:system_file:s0\n", encoding="utf-8")
        self.staged.write_text("/system/etc/a\\.b u:object_r:system_file:s0\n", encoding="utf-8")
        self.assertEqual(persist_context_rules(str(self.project), str(self.staged)), 0)

    def test_second_call_appends_nothing(self):
        self.project.write_text("/system u:object_r:rootfs:s0\n", encoding="utf-8")
        self.staged.write_text(
            "/system u:object_r:rootfs:s0\n"
            "/system/bin/new u:object_r:system_file:s0\n",
            encoding="utf-8",
        )
        self.assertEqual(persist_context_rules(str(self.project), str(self.staged)), 1)
        self.assertEqual(persist_context_rules(str(self.project), str(self.staged)), 0)

    def test_missing_project_file_is_reported(self):
        self.staged.write_text("/system u:object_r:rootfs:s0\n", encoding="utf-8")
        self.assertEqual(persist_context_rules(str(self.project), str(self.staged)), 0)
        self.assertFalse(self.project.exists())


if __name__ == "__main__":
    unittest.main()
