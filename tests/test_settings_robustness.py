"""Settings-file robustness regressions.

``load_setup_json`` truncated the settings file with ``open(..., "w")`` on every
start and parsed it without a guard, and ``validate_default_env_setup`` applied
``re.match`` to raw JSON values, so a numeric value raised ``TypeError``.  A
truncated file used to stop every CLI start with an unhandled JSONDecodeError.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Scripts.Primary import Settings
from Scripts.Primary.Utils import V


class SettingsRobustnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "art-res" / "settings.json"
        self.path.parent.mkdir(parents=True)
        saved = getattr(V, "SETUP_MANIFEST", None)
        self.addCleanup(setattr, V, "SETUP_MANIFEST", saved)
        patcher = mock.patch.object(Settings, "SETUP_JSON", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_truncated_settings_file_does_not_stop_startup(self):
        self.path.write_text('{"REPACK_EROFS_IMG": "1", "REPA', encoding="utf-8")
        Settings.load_setup_json()
        self.assertEqual(
            V.SETUP_MANIFEST["REPACK_EROFS_IMG"],
            Settings._SETUP_DEFAULTS["REPACK_EROFS_IMG"],
        )
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_numeric_setting_is_accepted_instead_of_raising(self):
        self.path.write_text(
            json.dumps({**Settings._SETUP_DEFAULTS, "REPACK_BR_LEVEL": 3}),
            encoding="utf-8",
        )
        Settings.load_setup_json()
        self.assertEqual(str(V.SETUP_MANIFEST["REPACK_BR_LEVEL"]), "3")

    def test_valid_settings_survive_a_round_trip(self):
        self.path.write_text(
            json.dumps({**Settings._SETUP_DEFAULTS, "SUPER_SIZE": "9126805504"}),
            encoding="utf-8",
        )
        Settings.load_setup_json()
        self.assertEqual(V.SETUP_MANIFEST["SUPER_SIZE"], "9126805504")
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["SUPER_SIZE"], "9126805504"
        )

    def test_writing_leaves_no_temporary_files(self):
        self.path.write_text(json.dumps(Settings._SETUP_DEFAULTS), encoding="utf-8")
        Settings.load_setup_json()
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != "settings.json"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
