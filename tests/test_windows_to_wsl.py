"""Regressions for the Windows-to-WSL path translation.

One drive prefix was rewritten per pass, so a string carrying two paths (the
busybox pipeline used by the WSL ramdisk packing, for instance) lost the
conversion of every path after the first and WSL could not address them.
"""
import unittest

from Scripts.Platform.runtime import ToolchainError, windows_to_wsl


class WindowsToWslTests(unittest.TestCase):
    def test_single_path_is_converted(self):
        self.assertEqual(windows_to_wsl(r"C:\Users\bai\OUT\system.img"),
                         "/mnt/c/Users/bai/OUT/system.img")

    def test_single_path_with_spaces_stays_one_path(self):
        self.assertEqual(windows_to_wsl(r"C:\Users\John Doe\OUT\system.img"),
                         "/mnt/c/Users/John Doe/OUT/system.img")

    def test_drive_letter_case_is_lowered(self):
        self.assertEqual(windows_to_wsl(r"D:\rom\boot.img"), "/mnt/d/rom/boot.img")

    def test_every_path_in_a_command_is_converted(self):
        command = "find . -mindepth 1 | C:/tools/busybox cpio -o -F C:/out/ramdisk.cpio"
        self.assertEqual(
            windows_to_wsl(command),
            "find . -mindepth 1 | /mnt/c/tools/busybox cpio -o -F /mnt/c/out/ramdisk.cpio")

    def test_several_paths_in_one_option_are_converted(self):
        self.assertEqual(windows_to_wsl(r"--file-contexts=C:\a\fc;C:\b\fc"),
                         "--file-contexts=/mnt/c/a/fc;/mnt/c/b/fc")

    def test_conversion_is_idempotent(self):
        converted = windows_to_wsl("C:/a C:/b")
        self.assertEqual(windows_to_wsl(converted), converted)

    def test_text_without_a_path_is_untouched(self):
        self.assertEqual(windows_to_wsl("ota patch --disable-avb"), "ota patch --disable-avb")
        self.assertEqual(windows_to_wsl("-R 0:0"), "-R 0:0")

    def test_extended_prefix_is_removed(self):
        # Utils.extended_path() produces \\?\C:\... for long paths; the prefix
        # used to survive as "\\?\/mnt/c/...".
        self.assertEqual(windows_to_wsl("\\\\?\\C:\\out\\system.img"),
                         "/mnt/c/out/system.img")
        self.assertEqual(windows_to_wsl("\\\\.\\D:\\rom"), "/mnt/d/rom")

    def test_unc_share_is_reported_instead_of_passed_through(self):
        with self.assertRaises(ToolchainError) as caught:
            windows_to_wsl("\\\\server\\share\\rom")
        message = str(caught.exception)
        self.assertIn("\\\\server\\share\\rom", message)
        self.assertIn("盘符", message)

    def test_extended_unc_share_is_reported(self):
        with self.assertRaises(ToolchainError):
            windows_to_wsl("\\\\?\\UNC\\server\\share\\rom")

    def test_drive_path_is_unaffected_by_the_unc_check(self):
        self.assertEqual(windows_to_wsl("C:\\rom"), "/mnt/c/rom")


if __name__ == "__main__":
    unittest.main()
