"""Regression for batch DAT decomposition failure isolation.

``decompress_dat_batch`` called ``future.result()`` unprotected, so a worker
raising outside its handled exception tuple (malformed transfer.list, decoder
error) aborted the whole batch before the summary and discarded the results of
every other partition.
"""
import contextlib
import io
import unittest
from unittest import mock

from Scripts.Extract import dat_br
from Scripts.Primary.Utils import V


class DatBatchFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self._jm = V.JM
        V.JM = True  # non-interactive: every discovered partition is selected
        self.addCleanup(setattr, V, "JM", self._jm)
        self.items = [
            {"path": "system.new.dat.br", "transfer": "system.transfer.list",
             "partition": "system", "size": 1},
            {"path": "vendor.new.dat.br", "transfer": "vendor.transfer.list",
             "partition": "vendor", "size": 1},
        ]

    def _run(self, worker):
        buffer = io.StringIO()
        with mock.patch.object(dat_br, "_list_dat_partitions", return_value=self.items):
            with mock.patch.object(dat_br, "_decompress_single_partition", worker):
                with contextlib.redirect_stdout(buffer):
                    dat_br.decompress_dat_batch(["ignored"], 2)
        return buffer.getvalue()

    def test_unexpected_worker_error_fails_one_partition_only(self):
        def worker(item, flag):
            if item["partition"] == "vendor":
                raise IndexError("transfer.list is truncated")
            return {"partition": item["partition"], "success": True, "error": None}

        output = self._run(worker)
        self.assertIn("分解完成: 1/2 成功", output)
        self.assertIn("system", output)
        self.assertIn("vendor", output)
        self.assertIn("IndexError", output)

    def test_reported_failures_are_counted(self):
        def worker(item, flag):
            return {"partition": item["partition"], "success": False, "error": "未生成分区"}

        output = self._run(worker)
        self.assertIn("分解完成: 0/2 成功", output)

    def test_all_success_is_reported(self):
        def worker(item, flag):
            return {"partition": item["partition"], "success": True, "error": None}

        output = self._run(worker)
        self.assertIn("分解完成: 2/2 成功", output)


if __name__ == "__main__":
    unittest.main()
