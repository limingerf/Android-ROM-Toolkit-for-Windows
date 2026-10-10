"""Manual check of automatic SELinux label completion during repack.

Run from the repository: python tests/native_context_smoke.py [path/to/art.exe]
Uses an isolated temporary project and never modifies an existing ROM.

The check repacks a labelled workspace twice: the first pass must not rewrite
any rule, and the second pass must complete the labels of the files and the
directory that were added afterwards.  The image is then extracted again so the
labels read back from the inode xattrs prove that e2fsdroid applied them.
"""
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
from Scripts.Extract.ext4 import extract_ext4
from Scripts.Platform.runtime import process_options

CONTEXTS = """\
/system u:object_r:rootfs:s0
/system(/.*)? u:object_r:rootfs:s0
/system/etc u:object_r:system_file:s0
/system/etc/init u:object_r:system_file:s0
/system/etc/init/keep\\.rc u:object_r:vendor_init_rc:s0
/system/etc/init/tool\\.bin u:object_r:system_file:s0
/system/etc/perms u:object_r:system_file:s0
/system/etc/perms/keep\\.xml u:object_r:mac_perms_file:s0
/system/etc/plain\\.txt u:object_r:system_file:s0
/system/bin u:object_r:system_file:s0
/system/bin/keep\\.sh u:object_r:system_file:s0
"""

# Sorted by image path, exactly as patch_contexts appends them.
EXPECTED = (
    "/system/etc/init/new\\.rc u:object_r:vendor_init_rc:s0",
    "/system/etc/new\\.conf u:object_r:system_file:s0",
    "/system/etc/newdir u:object_r:system_file:s0",
    "/system/etc/perms/new\\.xml u:object_r:mac_perms_file:s0",
)


def main():
    command = [str(Path(sys.argv[1]).resolve()), "--worker"] if len(sys.argv) > 1 else worker_command()
    resource_root = Path(command[0]).parent if len(sys.argv) > 1 else REPOSITORY
    with tempfile.TemporaryDirectory(prefix="art-context-smoke-") as temporary:
        root = Path(temporary) / "标签补齐测试"
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
        project = controller.create_project("context_smoke")["name"]
        layout = controller._layout(project)
        source = layout.workspace_dir / "system"
        for relative in ("etc/init", "etc/perms", "bin"):
            (source / relative).mkdir(parents=True, exist_ok=True)
        for relative in ("etc/init/keep.rc", "etc/init/tool.bin", "etc/perms/keep.xml",
                         "etc/plain.txt", "bin/keep.sh"):
            (source / relative).write_text("keep\n", encoding="utf-8")
        contexts = layout.config_dir / "system_contexts.txt"
        contexts.write_text(CONTEXTS, encoding="utf-8")
        (layout.config_dir / "system_fsconfig.txt").write_text("system 0 0 0755\n", encoding="utf-8")
        (layout.config_dir / "system_info.txt").write_text(json.dumps({
            "a": 16384, "b": 4096, "c": 32768, "d": "system", "s": 67108864,
        }), encoding="utf-8")

        def repack():
            request = controller.job_request("repack", project=project, partition="system",
                                             filesystem="ext", image_size="64")
            request["backend"] = "native"
            result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False) + "\n",
                                    capture_output=True, encoding="utf-8", errors="strict",
                                    env=env, timeout=180, **process_options())
            events = [json.loads(line) for line in result.stdout.splitlines()]
            logs = "\n".join(event.get("message", "") for event in events if event.get("event") == "log")
            assert not list(layout.workspace_dir.glob(".art-job-*")), "Worker left its temporary directory"
            return result, events, logs

        before = contexts.read_bytes()
        result, events, logs = repack()
        assert result.returncode == 0, logs + result.stderr
        assert "所有路径都已有标签，无需补充" in logs, logs
        assert contexts.read_bytes() == before, "metadata changed although every path was labelled"
        print("Labelled workspace repack: PASS (no rule rewritten)")

        for relative in ("etc/init/new.rc", "etc/perms/new.xml", "etc/new.conf"):
            (source / relative).write_text("new\n", encoding="utf-8")
        (source / "etc" / "newdir").mkdir()

        result, events, logs = repack()
        assert result.returncode == 0, logs + result.stderr
        assert "ContextPatcher: 补充 4 条标签" in logs, logs
        assert "已把 4 条补齐标签写回" in logs, logs
        appended = tuple(contexts.read_text(encoding="utf-8").splitlines()[-4:])
        assert appended == EXPECTED, appended
        print("Label completion: PASS (config siblings, plain siblings, directory)")

        readback = Path(temporary) / "readback"
        readback.mkdir()
        assert extract_ext4(layout.out_dir / "system.img", "system", readback / "system"), \
            "readback extraction failed"
        found = sorted(readback.rglob("*_contexts.txt"))
        assert found, "extractor wrote no contexts file"
        extracted = found[0].read_text(encoding="utf-8")
        for line in EXPECTED:
            assert line in extracted, f"label not applied in the image: {line}"
        print("Round trip: PASS (e2fsdroid applied every completed label)")


if __name__ == "__main__":
    main()
