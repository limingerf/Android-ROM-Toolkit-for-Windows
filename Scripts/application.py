"""Headless facade. Long operations run in isolated processes, never in Tk."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from Scripts.Platform.runtime import bootstrap_windows_tools, detect_toolchain, process_options
from Scripts.Primary.WorkSpace import ProjectLayout

ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent

def worker_command() -> list[str]:
    return [sys.executable, "--worker"] if getattr(sys, "frozen", False) else [sys.executable, str(ROOT / "Scripts" / "main.py"), "--worker"]

class ArtController:
    def __init__(self, root: str | os.PathLike[str] = ROOT):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.toolchain = detect_toolchain(self.root)

    def toolchain_status(self) -> dict:
        self.toolchain = detect_toolchain(self.root)
        return self.toolchain.diagnostics()

    def configure_tools(self, directory: str, backend: str = "native") -> dict:
        if backend not in {"native", "wsl"}:
            raise ValueError("后端应为 native 或 wsl")
        if directory and not Path(directory).is_dir():
            raise ValueError("工具目录不存在")
        config = self.root / "art-res" / "host-tools.local.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"backend": backend, "windows_tools": directory}, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.toolchain_status()

    def bootstrap_tools(self, progress=None) -> dict:
        result = bootstrap_windows_tools(self.root, progress=progress)
        self.toolchain = detect_toolchain(self.root)
        return result

    def _layout(self, project: str, *, create=False) -> ProjectLayout:
        path = Path(project).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("工程不能使用目录链接")
        path = path.resolve()
        if path.parent != self.root:
            raise ValueError("工程必须是工程根目录下的直接子目录")
        if not path.name.startswith("DNA_"):
            raise ValueError("工程应使用 DNA_ 前缀")
        layout = ProjectLayout(path)
        if create:
            return layout.initialize()
        if layout.state() != "new":
            raise ValueError("工程未初始化或使用了不兼容的目录布局")
        return layout

    def list_projects(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("DNA_*")):
            if path.is_symlink() or not path.is_dir():
                continue
            layout = ProjectLayout(path)
            result.append({"name": path.name, "path": str(path), "state": layout.state(),
                           "input_count": len(list(layout.input_dir.glob("*"))) if layout.input_dir.is_dir() else 0,
                           "output_count": len(list(layout.out_dir.glob("*"))) if layout.out_dir.is_dir() else 0,
                           "workspace_count": len(self.list_partitions(str(path))) if layout.state() == "new" else 0})
        return result

    def create_project(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("请输入工程名")
        if not name.startswith("DNA_"):
            name = "DNA_" + name
        ProjectLayout.validate_component(name, "工程")
        if name.endswith(".") or name.endswith(" "):
            raise ValueError("工程名不能以句点或空格结尾")
        layout = self._layout(name, create=True)
        return {"name": name, "path": str(layout.project_dir), "state": layout.state()}

    def inspect_input(self, source: str | os.PathLike[str]) -> dict:
        from Scripts.Primary.ImageTools import get_file_type
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        fmt = get_file_type(path)
        if path.suffix.lower() == ".zip" and fmt == "zip":
            result = {"path": str(path), "name": path.name, "size": path.stat().st_size, "format": "zip"}
            return result
        if path.name.endswith(".new.dat.br"):
            fmt = "dat.br"
        elif path.name.endswith(".new.dat"):
            fmt = "dat"
        elif ".win" in path.name:
            fmt = "win"
        result = {"path": str(path), "name": path.name, "size": path.stat().st_size, "format": fmt}
        if fmt == "payload":
            from Scripts.Extract.payload import info
            result["partitions"] = [{"name": name, "size": size} for name, size in info(path)]
        return result

    def list_inputs(self, project: str) -> list[dict]:
        return [self.inspect_input(p) for p in sorted(self._layout(project).input_dir.iterdir()) if p.is_file()]

    def list_partitions(self, project: str) -> list[dict]:
        layout = self._layout(project)
        result = []
        for path in sorted(layout.workspace_dir.iterdir()):
            if not path.is_dir() or path.name == "config" or path.name.startswith("."):
                continue
            info = layout.config_dir / (path.name + "_info.txt")
            fmt = "ext" if info.is_file() else "erofs" if (layout.config_dir / (path.name + "_size.txt")).is_file() else "boot" if (path / "boot_o.img").is_file() else "unknown"
            result.append({"name": path.name, "path": str(path), "format": fmt})
        return result

    def import_inputs(self, project: str, sources: list[str]) -> list[str]:
        layout = self._layout(project)
        # DAT packages need their transfer list. If the user selects only the
        # .dat/.dat.br file, bring the sibling list along automatically.
        expanded = []
        for source in sources:
            source_path = Path(source).expanduser().resolve()
            expanded.append(source_path)
            if source_path.name.endswith((".new.dat", ".new.dat.br")):
                prefix = source_path.name.split(".new.dat", 1)[0]
                for companion in sorted(source_path.parent.glob(prefix + ".transfer.list")):
                    if companion not in expanded:
                        expanded.append(companion)
        pending = []
        for path in expanded:
            if not path.is_file():
                raise FileNotFoundError(path)
            destination = layout.input_dir / path.name
            if destination.exists() and destination.resolve() != path:
                raise FileExistsError(f"INPUT 已有同名文件：{path.name}")
            pending.append((path, destination))
        imported = []
        for path, destination in pending:
            if path != destination.resolve():
                with tempfile.NamedTemporaryFile(dir=layout.input_dir, prefix=".import-", delete=False) as file:
                    temp = Path(file.name)
                try:
                    shutil.copyfile(path, temp)
                    os.replace(temp, destination)
                finally:
                    temp.unlink(missing_ok=True)
            imported.append(str(destination))
        return imported

    def import_rom_archive(self, project: str, source: str) -> list[str]:
        """Import supported files from a ROM ZIP without extracting unsafe paths."""
        import tempfile
        import zipfile
        from Scripts.Primary.Utils import safe_extract_zip

        layout = self._layout(project)
        archive = Path(source).expanduser().resolve()
        if not archive.is_file() or not zipfile.is_zipfile(archive):
            raise ValueError("请选择有效的 ROM ZIP 文件")
        with tempfile.TemporaryDirectory(prefix=".art-import-", dir=layout.workspace_dir) as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(archive) as handle:
                safe_extract_zip(handle, root)
            supported = []
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                name = path.name.lower()
                if (name == "payload.bin" or name.endswith((".img", ".win", ".win.001", ".new.dat", ".new.dat.br",
                                                              ".transfer.list", ".patch.dat"))):
                    supported.append(path)
            if not supported:
                raise ValueError("ZIP 中未找到 payload.bin、镜像或 DAT 文件")
            return self.import_inputs(project, [str(path) for path in supported])

    def list_outputs(self, project: str) -> list[dict]:
        layout = self._layout(project)
        return [{"name": path.name, "path": str(path), "size": path.stat().st_size}
                for path in sorted(layout.out_dir.iterdir()) if path.is_file()]

    def job_request(self, operation: str, **params) -> dict:
        if operation not in {"extract", "repack", "repack_batch", "repack_super", "convert"}:
            raise ValueError("未知操作")
        if "project" in params:
            params["project"] = str(self._layout(params["project"]).project_dir)
        return {"root": str(self.root), "operation": operation, "params": params}

    def start_job(self, request: dict):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                   env=env, cwd=ROOT, **process_options())
        process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        process.stdin.close()
        return process

    def run_job(self, operation: str, **params) -> dict:
        process = self.start_job(self.job_request(operation, **params))
        result = None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "result":
                result = event.get("data")
            elif event.get("event") == "error":
                result = {"error": event.get("message")}
        code = process.wait()
        if code or result is None or "error" in result:
            raise RuntimeError((result or {}).get("error", f"任务异常退出：{code}"))
        return result

    def extract_images(self, project: str, sources: list[str] | None = None) -> dict:
        return self.run_job("extract", project=project, sources=sources)

    def repack_partition(self, project: str, partition: str, sparse: bool = False) -> dict:
        return self.run_job("repack", project=project, partition=partition, sparse=sparse)

    def repack_super(self, project: str, sources: list[str], super_type: int = 0,
                     sparse: bool = False) -> dict:
        return self.run_job("repack_super", project=project, sources=sources,
                            super_type=super_type, sparse=sparse)

    def convert_image(self, project: str, source: str, target: str) -> dict:
        return self.run_job("convert", project=project, source=source, target=target)
