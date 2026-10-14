"""Regressions for the release builder.

``--add-data`` was hard-coded with the Windows separator, so a POSIX build got
a broken resource layout.  ``copy_release_resources`` copied the whole
``art-res`` tree, including the Linux/arm64 toolchain that no code path in
``Scripts/`` references, and it raised FileNotFoundError when ``art-res`` was
missing because the target directory was only created by the copy itself.
"""
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import build


class PyInstallerCommandTests(unittest.TestCase):
    def _add_data(self, command):
        return [command[index + 1] for index, item in enumerate(command)
                if item == '--add-data']

    def test_windows_separator_is_used_by_default_on_windows(self):
        self.assertEqual(build._data_separator(), ';' if os.name == 'nt' else ':')

    def test_posix_separator_is_applied_to_every_entry(self):
        entries = self._add_data(build.pyinstaller_command(separator=':'))
        self.assertEqual(len(entries), 3)
        self.assertTrue(entries[0].endswith(':assets'))
        self.assertTrue(entries[2].endswith(':assets/keys'))
        for entry in entries:
            self.assertNotIn(';', entry)

    def test_windows_separator_is_applied_to_every_entry(self):
        entries = self._add_data(build.pyinstaller_command(separator=';'))
        self.assertEqual(len(entries), 3)
        self.assertTrue(entries[0].endswith(';assets'))
        self.assertTrue(entries[2].endswith(';assets/keys'))

    def test_destination_has_no_leading_separator(self):
        for entry in self._add_data(build.pyinstaller_command(separator=':')):
            destination = entry.rsplit(':', 1)[1]
            self.assertFalse(destination.startswith('/'))

    def test_entry_point_and_hidden_imports_are_present(self):
        command = build.pyinstaller_command()
        self.assertIn(str(build.ROOT / 'Scripts' / 'main.py'), command)
        self.assertIn('Scripts.mcp_server', command)
        self.assertIn(sys.executable, command)


class CopyReleaseResourcesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.release = self.root / 'release'
        self.release.mkdir()
        self._root_patch = mock.patch.object(build, 'ROOT', self.root)
        self._root_patch.start()
        self.addCleanup(self._root_patch.stop)

    def _make_art_res(self):
        art_res = self.root / 'art-res'
        (art_res / 'bin-win-amd64').mkdir(parents=True)
        (art_res / 'bin-win-amd64' / 'mke2fs.exe').write_bytes(b'x')
        (art_res / 'bin-arm64').mkdir()
        (art_res / 'bin-arm64' / 'avbroot').write_bytes(b'y')
        (art_res / 'bin-amd64').mkdir()
        (art_res / 'bin-amd64' / 'avbroot').write_bytes(b'z')
        (art_res / 'settings.json').write_text('{}', encoding='utf-8')
        (art_res / 'ui-settings.json').write_text('{}', encoding='utf-8')
        (art_res / 'host-tools.local.json').write_text('{}', encoding='utf-8')
        return art_res

    def test_every_tool_directory_is_shipped(self):
        # The tool directory is resolved at runtime as art-res/bin-{arch} (WSL)
        # and art-res/bin-win-{arch} (native), and _architecture() reports arm64
        # on Windows-on-ARM, so pruning a bin-* directory breaks that host.
        self._make_art_res()
        build.copy_release_resources(self.release)
        target = self.release / 'art-res'
        self.assertTrue((target / 'bin-arm64' / 'avbroot').is_file())
        self.assertTrue((target / 'bin-win-amd64' / 'mke2fs.exe').is_file())
        self.assertTrue((target / 'bin-amd64' / 'avbroot').is_file())

    def test_machine_local_settings_are_not_shipped(self):
        self._make_art_res()
        build.copy_release_resources(self.release)
        target = self.release / 'art-res'
        self.assertTrue((target / 'settings.json').is_file())
        self.assertFalse((target / 'ui-settings.json').exists())
        self.assertFalse((target / 'host-tools.local.json').exists())

    def test_missing_art_res_does_not_raise(self):
        build.copy_release_resources(self.release)
        self.assertTrue((self.release / 'art-res').is_dir())


class ZipReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.release = self.root / 'release'
        (self.release / 'art-res' / 'bin').mkdir(parents=True)
        (self.release / 'art-res' / 'bin' / 'mke2fs.exe').write_bytes(b'x')
        (self.release / 'art.exe').write_bytes(b'MZ')

    def test_archive_uses_forward_slashes_and_counts_files(self):
        archive = self.root / 'release.zip'
        count = build.zip_release(self.release, archive)
        self.assertEqual(count, 2)
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
        self.assertIn('art.exe', names)
        self.assertIn('art-res/bin/mke2fs.exe', names)
        self.assertFalse([name for name in names if '\\' in name])

    def test_existing_archive_is_replaced(self):
        archive = self.root / 'release.zip'
        archive.write_bytes(b'stale')
        build.zip_release(self.release, archive)
        with zipfile.ZipFile(archive) as handle:
            self.assertIn('art.exe', handle.namelist())


class RemoveGeneratedPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_directory_is_removed(self):
        target = self.root / 'tree'
        (target / 'sub').mkdir(parents=True)
        build.remove_generated_path(target)
        self.assertFalse(target.exists())

    def test_file_is_removed(self):
        target = self.root / 'art.exe'
        target.write_bytes(b'MZ')
        build.remove_generated_path(target)
        self.assertFalse(target.exists())

    def test_missing_path_is_a_no_op(self):
        build.remove_generated_path(self.root / 'absent')


class RunPyInstallerTests(unittest.TestCase):
    """PyInstaller output must survive a non-UTF-8 console code page.

    On a Chinese Windows host PyInstaller writes GBK bytes.  ``text=True``
    without an explicit encoding decoded them as UTF-8, raised
    UnicodeDecodeError inside ``subprocess.run`` and killed the build before it
    reported anything.  The stream is now decoded as UTF-8 with ``replace``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _emitter(self):
        """A child process that writes GBK bytes, as PyInstaller does."""
        script = self.root / 'gbk_emitter.py'
        script.write_text(
            'import sys\n'
            "sys.stdout.buffer.write(b'\\xd5\\xfd\\xd4\\xda done\\n')\n"
            'sys.stdout.buffer.flush()\n',
            encoding='utf-8',
        )
        return [sys.executable, str(script)]

    def test_gbk_console_bytes_are_replaced_not_raised(self):
        with mock.patch.object(build, 'pyinstaller_command', self._emitter):
            result = build.run_pyinstaller()
        self.assertEqual(result.returncode, 0)
        self.assertIn('done', result.stdout)
        self.assertIn('\ufffd', result.stdout)

    def test_child_gets_utf8_stdio(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return build.subprocess.CompletedProcess(command, 0, '', None)

        with mock.patch.object(build.subprocess, 'run', fake_run):
            build.run_pyinstaller()
        self.assertEqual(captured['encoding'], 'utf-8')
        self.assertEqual(captured['errors'], 'replace')
        self.assertEqual(captured['env'].get('PYTHONIOENCODING'), 'utf-8')


class BuildMainLogTests(unittest.TestCase):
    """The build log is written even when the console stream was not UTF-8."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name, target in (('ROOT', self.root),
                             ('BUILD_DIR', self.root / '.art-build'),
                             ('DIST_DIR', self.root / '.art-dist'),
                             ('RELEASE_DIR', self.root / '.art-release')):
            patch = mock.patch.object(build, name, target)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(lambda: sys.argv.__setitem__(slice(None), self._argv))
        self._argv = list(sys.argv)

    def test_main_writes_the_log_with_replacement(self):
        # The console stream carried a 0xb7 byte, which is not valid UTF-8; the
        # log must still be written instead of aborting the build.
        def fake_run_pyinstaller():
            executable = build.DIST_DIR / ('art.exe' if os.name == 'nt' else 'art')
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b'MZ')
            # GBK bytes for 正在, which are not valid UTF-8.
            stdout = b'\xd5\xfd\xd4\xda ok\n'.decode('utf-8', 'replace')
            return build.subprocess.CompletedProcess(['pyinstaller'], 0, stdout, None)

        def fake_zip_release(release_dir, archive_path):
            archive_path.write_bytes(b'PK')
            return 1

        with mock.patch.object(build, 'run_pyinstaller', fake_run_pyinstaller), \
                mock.patch.object(build, 'copy_release_resources'), \
                mock.patch.object(build, 'zip_release', fake_zip_release):
            sys.argv = ['build.py']
            build.main()

        log = (self.root / 'build-call.log').read_text(encoding='utf-8')
        self.assertIn('ok', log)
        self.assertIn('\ufffd', log)


if __name__ == '__main__':
    unittest.main()
