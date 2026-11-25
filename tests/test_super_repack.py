"""Super repack regressions: atomic output, B slot lookup, duplicate slots.

``repack_super`` published whatever lpmake left in OUT.  A failing lpmake
therefore "produced" a truncated ``super.img.raw`` that the worker happily
copied to the project, and a failed sparse conversion had the same shape.
The A/B builder only looked for ``<name>_b.img``, so every sparse bundle got a
zero-sized B slot, and selecting both slots defined the same partition twice.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from Scripts.Primary.Utils import V
from Scripts.ReMake import super as supermod
from Scripts.worker import _repack_super


class SuperRepackTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.input = self.root / "input"
        self.out = self.root / "out"
        self.workspace = self.root / "workspace"
        for directory in (self.input, self.out, self.workspace):
            directory.mkdir()
        saved = {name: getattr(V, name, None) for name in ("input", "out", "workspace", "SETUP_MANIFEST")}
        self.addCleanup(self._restore, saved)
        V.input = str(self.input) + os.sep
        V.out = str(self.out) + os.sep
        V.workspace = str(self.workspace) + os.sep
        V.SETUP_MANIFEST = {"GROUP_NAME": "group", "SUPER_SIZE": "1024"}

    def _restore(self, saved):
        for name, value in saved.items():
            if value is None:
                try:
                    delattr(V, name)
                except AttributeError:
                    pass
            else:
                setattr(V, name, value)

    def _image(self, name, content=b"image-data"):
        path = self.input / name
        path.write_bytes(content)
        return path

    @staticmethod
    def _fake_lpmake(returncode=0):
        def run(argv):
            output = Path(argv[argv.index("--out") + 1])
            output.write_bytes(b"PARTIAL-CONTAINER")
            return returncode
        return run


class AtomicOutputTests(SuperRepackTestCase):
    def test_lpmake_failure_keeps_the_previous_super_and_removes_partial(self):
        previous = self.out / "super.img"
        previous.write_bytes(b"PREVIOUS-GOOD-IMAGE")
        source = self._image("system.img")
        with mock.patch.object(supermod, "call", self._fake_lpmake(returncode=1)):
            self.assertFalse(supermod.repack_super([("system", str(source))], 0, 0))
        self.assertEqual(previous.read_bytes(), b"PREVIOUS-GOOD-IMAGE")
        self.assertFalse((self.out / ".super.img.build").exists())
        self.assertFalse((self.out / "super.img.raw").exists())

    def test_sparse_conversion_failure_keeps_the_previous_super(self):
        previous = self.out / "super.img"
        previous.write_bytes(b"PREVIOUS-GOOD-IMAGE")
        source = self._image("system.img")
        with mock.patch.object(supermod, "call", self._fake_lpmake()), \
                mock.patch.object(supermod, "raw_to_sparse",
                                  side_effect=OSError("disk full")):
            self.assertFalse(supermod.repack_super([("system", str(source))], 0, 1))
        self.assertEqual(previous.read_bytes(), b"PREVIOUS-GOOD-IMAGE")
        self.assertFalse((self.out / ".super.img.build").exists())


class ABSlotTests(SuperRepackTestCase):
    def _record_lpmake(self):
        calls = []

        def run(argv):
            calls.append(list(argv))
            Path(argv[argv.index("--out") + 1]).write_bytes(b"CONTAINER")
            return 0
        return calls, run

    def test_sparse_b_slot_is_looked_up_and_sized(self):
        source = self._image("system_a.img", b"A" * 4096)
        self._image("system_b.sparse.img", b"sparse-b")

        def fake_is_sparse(path):
            return str(path).endswith(".sparse.img")

        def fake_sparse_to_raw(path, destination, temp_dir=None):
            Path(destination).write_bytes(b"B" * 4096)
            return str(destination)

        calls, run = self._record_lpmake()
        with mock.patch.object(supermod, "is_sparse_image", fake_is_sparse), \
                mock.patch.object(supermod, "sparse_to_raw", fake_sparse_to_raw), \
                mock.patch.object(supermod, "call", run):
            self.assertTrue(supermod.repack_super([("system", str(source))], 1, 0))
        argv = calls[0]
        partitions = [argv[index + 1] for index, value in enumerate(argv) if value == "--partition"]
        self.assertIn("system_a:readonly:4096:group_a", partitions)
        self.assertIn("system_b:readonly:4096:group_b", partitions)
        self.assertFalse(any(partition.startswith("system_b:readonly:0:") for partition in partitions))
        self.assertTrue((self.out / "super.img").is_file())
        self.assertEqual(list(self.workspace.glob(".*raw.img")), [])

    def test_duplicate_slot_selection_defines_each_partition_once(self):
        first = self._image("system_a.img", b"A" * 4096)
        second = self._image("system_b.img", b"B" * 4096)
        calls, run = self._record_lpmake()
        with mock.patch.object(supermod, "call", run):
            self.assertTrue(supermod.repack_super(
                [("system", str(first)), ("system", str(second))], 1, 0))
        argv = calls[0]
        partitions = [argv[index + 1] for index, value in enumerate(argv) if value == "--partition"]
        self.assertEqual(len([p for p in partitions if p.startswith("system_a:")]), 1)
        self.assertEqual(len([p for p in partitions if p.startswith("system_b:")]), 1)


class WorkerSuperStagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ("input", "out", "workspace", "stage-input", "stage-out"):
            (self.root / name).mkdir()
        self.layout = SimpleNamespace(
            input_dir=self.root / "input", out_dir=self.root / "out",
            workspace_dir=self.root / "workspace")
        self.stage = SimpleNamespace(
            input_dir=self.root / "stage-input", out_dir=self.root / "stage-out")

    def test_worker_stages_both_slots_and_passes_one_entry(self):
        first = self.layout.input_dir / "system_a.img"
        first.write_bytes(b"A" * 4096)
        second = self.layout.input_dir / "system_b.sparse.img"
        second.write_bytes(b"sparse-b")
        captured = {}

        def fake_repack(parts, super_type, sparse):
            captured["parts"] = list(parts)
            return False

        with mock.patch("Scripts.ReMake.super.repack_super", fake_repack):
            with self.assertRaises(RuntimeError):
                _repack_super(None, self.layout, self.stage, {
                    "sources": [str(first), str(second)], "super_type": 1,
                })
        self.assertEqual(captured["parts"], [("system", str(self.stage.input_dir / "system_a.img"))])
        self.assertTrue((self.stage.input_dir / "system_a.img").is_file())
        self.assertTrue((self.stage.input_dir / "system_b.sparse.img").is_file())

    def test_worker_refuses_a_result_without_super_img(self):
        first = self.layout.input_dir / "system.img"
        first.write_bytes(b"A" * 4096)
        with mock.patch("Scripts.ReMake.super.repack_super", lambda *args: True):
            with self.assertRaises(RuntimeError):
                _repack_super(None, self.layout, self.stage,
                              {"sources": [str(first)], "super_type": 0})


if __name__ == "__main__":
    unittest.main()
