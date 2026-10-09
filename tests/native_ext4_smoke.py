"""Manual Windows smoke check of the real worker, tools, and rollback.

Run from the repository: python tests/native_ext4_smoke.py [path/to/art.exe]
Uses an isolated temporary project and never modifies an existing ROM.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
from Scripts.application import ArtController, worker_command
from Scripts.Platform.runtime import process_options


def main():
    command = [str(Path(sys.argv[1]).resolve()), "--worker"] if len(sys.argv) > 1 else worker_command()
    resource_root = Path(command[0]).parent if len(sys.argv) > 1 else REPOSITORY
    with tempfile.TemporaryDirectory(prefix="art-native-smoke-") as temporary:
        root = Path(temporary) / "原生回包测试"
        tools = root / "art-res" / "bin-win-amd64"
        tools.mkdir(parents=True)
        for name in ("mke2fs.exe", "e2fsdroid.exe", "cygwin1.dll"):
            shutil.copy2(resource_root / "art-res" / "bin-win-amd64" / name, tools / name)
        shutil.copytree(resource_root / "art-res" / "bin-win-amd64" / "e2fsprogs", tools / "e2fsprogs")
        env = os.environ.copy()
        env.update({"ART_BACKEND": "native", "ART_WINDOWS_TOOLS": "", "PYTHONUTF8": "1"})
        # A packaged check must not borrow tools/DLLs from the developer's PATH.
        if len(sys.argv) > 1:
            windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            env["PATH"] = str(windows / "System32") + os.pathsep + str(windows)
            for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
                env.pop(key, None)
        controller = ArtController(root)
        project = controller.create_project("native_smoke")["name"]
        layout = controller._layout(project)
        source = layout.workspace_dir / "system"
        (source / "etc").mkdir(parents=True)
        (source / "etc" / "hal.txt").write_text("native EXT4 worker verification\n", encoding="utf-8")
        (layout.config_dir / "system_fsconfig.txt").write_text("system 0 0 0755\n", encoding="utf-8")
        (layout.config_dir / "system_contexts.txt").write_text("/system(/.*)? u:object_r:system_file:s0\n", encoding="utf-8")
        (layout.config_dir / "system_info.txt").write_text(json.dumps({
            "a": 16384, "b": 4096, "c": 32768, "d": "system", "s": 67108864,
        }), encoding="utf-8")

        def invoke(size):
            request = controller.job_request("repack", project=project, partition="system",
                                             filesystem="ext", image_size=size)
            request["backend"] = "native"
            result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False) + "\n",
                                    capture_output=True, encoding="utf-8", errors="strict",
                                    env=env, timeout=120, **process_options())
            events = [json.loads(line) for line in result.stdout.splitlines()]
            logs = "\n".join(event.get("message", "") for event in events if event.get("event") == "log")
            assert "工具后端：Windows 原生 · e2fsck.exe" in logs or result.returncode != 0, logs
            assert "wsl:" not in logs.lower() and "攀昲捳" not in logs and "\ufffd" not in logs, logs
            assert "\x00" not in logs and "\b" not in logs, logs
            assert not list(layout.workspace_dir.glob(".art-job-*")), "Worker left its temporary directory"
            return result, events, logs

        result, events, logs = invoke("64")
        assert result.returncode == 0, logs + result.stderr
        completed = [event for event in events if event.get("event") == "result"]
        assert len(completed) == 1 and not any(event.get("event") == "error" for event in events), events
        assert "e2fsck -fn 通过" in completed[0]["data"]["validation"], completed
        image = layout.out_dir / "system.img"
        assert image.stat().st_size == 67108864
        before = hashlib.sha256(image.read_bytes()).hexdigest()
        print("Real native worker repack: PASS (64 MiB, Unicode project path, e2fsck -fn)")

        # Force e2fsdroid to run out of space. A previous successful image
        # must survive, and a tool failure must not be reported as a result.
        (source / "etc" / "oversized.dat").write_bytes(b"x" * (2 * 1024 * 1024))
        result, events, logs = invoke("1")
        assert "工具后端：Windows 原生 · e2fsdroid.exe" in logs, logs
        assert result.returncode != 0, logs
        assert any(event.get("event") == "error" for event in events), events
        assert not any(event.get("event") == "result" for event in events), events
        assert hashlib.sha256(image.read_bytes()).hexdigest() == before
        assert not list(layout.out_dir.glob("*_new.img"))
        print("e2fsdroid failure and temporary output cleanup: PASS (previous image preserved)")


if __name__ == "__main__":
    main()
