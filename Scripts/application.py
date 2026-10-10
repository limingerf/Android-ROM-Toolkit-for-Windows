"""Headless facade. Long operations run in isolated worker processes."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from Scripts.Platform.runtime import bootstrap_windows_tools, detect_toolchain, process_options
from Scripts.Primary.WorkSpace import ProjectLayout, classify_ota_parts
from Scripts.Primary.Settings import _SETUP_DEFAULTS
from Scripts.Primary.Utils import remove_tree


def _ota_image_args(images, existing, partition_sizes):
    """Build the ``--replace``/``--add-partition`` arguments for staged images.

    ``Path.stem`` kept the ``.sparse`` suffix and the ``_a`` slot, so a staged
    ``system.sparse.img`` was offered to avbroot as a brand new partition
    instead of a replacement.  Returns (args, added_names, staged_names).
    """
    replace_parts, new_parts = classify_ota_parts(
        [image.name for image in images], existing)
    by_name = {image.name: image for image in images}
    args = []
    for part, image_name in replace_parts:
        args.extend(["--replace", part, str(by_name[image_name])])
    added = []
    for part, image_name, _ in new_parts:
        args.extend(["--add-partition", part, str(by_name[image_name])])
        # Accept the size keyed by either the OTA name or the file stem.
        size = partition_sizes.get(part)
        if size is None:
            size = partition_sizes.get(Path(image_name).stem)
        if size is not None:
            args.append(str(size))
        added.append(part)
    staged_names = {image.stem for image in images} | {name for name, _, _ in new_parts}
    return args, added, staged_names


def _write_json_atomic(path: Path, payload: str) -> None:
    """Write JSON so an interrupted write cannot leave a truncated file.

    The previous plain ``write_text`` truncated first, so a kill, a full disk or
    a second instance left unparsable JSON that then broke every later job.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                         dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

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
        _write_json_atomic(
            config,
            json.dumps({"backend": backend, "windows_tools": directory}, ensure_ascii=False, indent=2),
        )
        # An explicit GUI/MCP choice also overrides an inherited environment
        # variable for this process and its workers.
        os.environ["ART_BACKEND"] = backend
        os.environ["ART_WINDOWS_TOOLS"] = directory
        return self.toolchain_status()

    def bootstrap_tools(self, progress=None) -> dict:
        result = bootstrap_windows_tools(self.root, progress=progress)
        self.toolchain = detect_toolchain(self.root)
        return result

    # The interactive CLI exposes project deletion, setup editing and
    # selective payload/super inspection.  Keep these operations in the
    # controller so the Qt front end and MCP use the same path validation and
    # never have to reach into the legacy ``V`` globals.
    def delete_project(self, project: str) -> dict:
        """Delete a project after enforcing the direct-child/safe-layout rule."""
        project_dir = Path(project).expanduser()
        if not project_dir.is_absolute():
            project_dir = self.root / project_dir
        try:
            project_dir = project_dir.resolve()
            project_dir.relative_to(self.root)
        except ValueError as error:
            raise ValueError("工程必须位于工程根目录下") from error
        if project_dir.parent != self.root or not project_dir.name.startswith("DNA_"):
            raise ValueError("工程名称或路径无效")
        if project_dir.is_symlink() or not project_dir.is_dir():
            raise ValueError("工程目录不存在或不是目录")
        remove_tree(project_dir)
        return {"name": project_dir.name, "deleted": True}

    def get_settings(self) -> dict:
        """Read the CLI manifest, filling newly introduced defaults."""
        path = self.root / "art-res" / "settings.json"
        values = dict(_SETUP_DEFAULTS)
        try:
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    values.update({str(k): str(v) for k, v in loaded.items()})
        except (OSError, ValueError, TypeError):
            pass
        return values

    def update_settings(self, updates: dict) -> dict:
        """Persist supported CLI settings and reject unknown keys."""
        if not isinstance(updates, dict):
            raise ValueError("设置必须是对象")
        values = self.get_settings()
        unknown = sorted(set(updates) - set(_SETUP_DEFAULTS))
        if unknown:
            raise ValueError(f"不支持的设置项：{', '.join(unknown)}")
        values.update({str(k): str(v) for k, v in updates.items()})
        # Mirror Settings.validate_default_env_setup without terminating the
        # GUI process via sys.exit().
        for key in ("REPACK_EROFS_IMG", "REPACK_SPARSE_IMG", "REPACK_TO_RW", "RESIZE_IMG"):
            if values.get(key) not in {"0", "1"}:
                raise ValueError(f"{key} 必须为 0 或 1")
        if values.get("RESIZE_EROFSIMG") not in {"0", "1", "2"}:
            raise ValueError("RESIZE_EROFSIMG 必须为 0、1 或 2")
        try:
            if not 0 <= int(values.get("REPACK_BR_LEVEL", "3")) <= 9:
                raise ValueError
            if not 1 <= int(values.get("UNPACK_SPLIT_DAT", "15")) <= 999:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("BROTLI 等级应为 0-9，DAT 分段数应为 1-999")
        path = self.root / "art-res" / "settings.json"
        _write_json_atomic(path, json.dumps(values, ensure_ascii=False, indent=4))
        return values

    def payload_partitions(self, project: str, source: str) -> list[dict]:
        """Return payload partition names/sizes for a GUI selection dialog."""
        layout = self._layout(project)
        path = Path(source).expanduser().resolve()
        if path.parent != layout.input_dir.resolve() or not path.is_file():
            raise ValueError("Payload 必须位于该工程 INPUT 目录")
        if self.inspect_input(path).get("format") != "payload":
            raise ValueError("选择的文件不是 payload.bin")
        from Scripts.Extract.payload import info
        return [{"name": name, "size": size} for name, size in info(path)]

    def super_partitions(self, project: str, source: str) -> list[str]:
        """Return logical partitions in a super image for selective extraction."""
        layout = self._layout(project)
        path = Path(source).expanduser().resolve()
        if path.parent != layout.input_dir.resolve() or not path.is_file():
            raise ValueError("super 镜像必须位于该工程 INPUT 目录")
        if self.inspect_input(path).get("format") != "super":
            raise ValueError("选择的文件不是 super.img")
        from Scripts.Primary.SuperTools import LpUnpack, _SparseRawCache
        # Keep sparse conversion files in the workspace temp area and always
        # clear the shared cache after the metadata-only query.  Without this
        # cleanup a second GUI query could reuse a deleted path.
        unpacker = LpUnpack(SUPER_IMAGE=str(path), OUTPUT_DIR=None, SHOW_INFO=False,
                            TEMP_DIR=str(layout.workspace_dir))
        try:
            return list(unpacker.get_info())
        finally:
            unpacker.close()
            _SparseRawCache.cleanup_all()

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

    def list_plugins(self) -> list[dict]:
        """List installed CLI submodules without importing or executing them."""
        root = self.root / "local" / "sub"
        if not root.is_dir():
            return []
        result = []
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.is_symlink() and (path / "run.sh").is_file():
                result.append({"name": path.name, "path": str(path), "entry": str(path / "run.sh")})
        return result

    def install_plugin(self, source: str, *, replace: bool = True) -> dict:
        """Install a CLI plugin ZIP after validating its entry script and paths."""
        import tempfile
        import zipfile
        from Scripts.Primary.Utils import safe_extract_zip
        archive = self._require_file(source, "插件 ZIP")
        if not zipfile.is_zipfile(archive):
            raise ValueError("请选择有效的插件 ZIP")
        plugin_root = self.root / "local" / "sub"
        plugin_root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in archive.stem).strip("._-") or "plugin"
        name = safe_name if safe_name.startswith("DNA_") else "DNA_" + safe_name
        ProjectLayout.validate_component(name, "插件")
        with tempfile.TemporaryDirectory(prefix=".art-plugin-", dir=plugin_root) as temporary:
            staged_root = Path(temporary)
            with zipfile.ZipFile(archive) as handle:
                safe_extract_zip(handle, staged_root)
            entries = [path for path in staged_root.rglob("run.sh") if path.is_file()]
            if len(entries) != 1:
                raise ValueError("插件 ZIP 必须包含唯一的 run.sh")
            staged = entries[0].parent
            destination = plugin_root / name
            if destination.exists() or destination.is_symlink():
                if not replace:
                    raise FileExistsError(f"插件已存在：{name}")
                if destination.is_symlink() or not destination.is_dir():
                    destination.unlink()
                else:
                    remove_tree(destination)
            shutil.copytree(staged, destination)
        return {"name": name, "path": str(destination), "installed": True}

    def remove_plugin(self, name: str) -> dict:
        name = ProjectLayout.validate_component(name, "插件")
        if not name.startswith("DNA_"):
            raise ValueError("插件名称必须使用 DNA_ 前缀")
        destination = self.root / "local" / "sub" / name
        if not destination.is_dir() or destination.is_symlink():
            raise FileNotFoundError(f"插件不存在：{name}")
        remove_tree(destination)
        return {"name": name, "removed": True}

    # ---- AVB / OTA non-interactive helpers -----------------------------
    def _tool_capture(self, tool: str, args: list[str], *, cwd=None) -> dict:
        """Run a bundled host tool and return a stable GUI-friendly result."""
        command = self.toolchain.command([tool, *[str(item) for item in args]], bundled=True)
        process = self.toolchain.capture([tool, *[str(item) for item in args]],
                                        cwd=str(cwd) if cwd else None,
                                        capture_output=True, text=True)
        output = (process.stdout or "") + (process.stderr or "")
        return {"ok": process.returncode == 0, "code": process.returncode,
                "command": command, "output": output.strip()}

    @staticmethod
    def _require_file(path: str | os.PathLike[str], label: str = "文件") -> Path:
        value = Path(path).expanduser().resolve()
        if not value.is_file():
            raise FileNotFoundError(f"{label}不存在：{value}")
        return value

    def avb_info(self, image: str) -> dict:
        image_path = self._require_file(image, "镜像")
        result = self._tool_capture("avbtool", ["info_image", "--image", image_path])
        result.update({"operation": "info", "image": str(image_path)})
        if not result["ok"]:
            raise RuntimeError(result["output"] or f"avbtool 退出码 {result['code']}")
        return result

    def avb_verify(self, image: str) -> dict:
        image_path = self._require_file(image, "镜像")
        result = self._tool_capture("avbtool", ["verify_image", "--image", image_path])
        result.update({"operation": "verify", "image": str(image_path), "verified": bool(result["ok"])})
        return result

    def avb_erase_footer(self, image: str, output: str | None = None) -> dict:
        image_path = self._require_file(image, "镜像")
        target = Path(output).expanduser().resolve() if output else image_path.with_name(image_path.stem + "_unsign" + image_path.suffix)
        if target == image_path:
            raise ValueError("输出文件不能覆盖输入镜像，请指定新文件名")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".art-avb-", dir=target.parent) as stage_dir:
            staged = Path(stage_dir) / target.name
            shutil.copy2(image_path, staged)
            result = self._tool_capture("avbtool", ["erase_footer", "--image", staged])
            if not result["ok"]:
                raise RuntimeError(result["output"] or f"avbtool 退出码 {result['code']}")
            if not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError("AVB 操作未生成有效镜像")
            os.replace(staged, target)
        result.update({"operation": "erase_footer", "image": str(image_path), "output": str(target)})
        return result

    def avb_add_footer(self, image: str, *, kind: str = "hash", partition_name: str | None = None,
                       partition_size: int | None = None, algorithm: str | None = None,
                       key: str | None = None, output: str | None = None,
                       rollback_index: int = 0) -> dict:
        from Scripts.signing import resolve_avb_key, avb_algorithm_for_key

        image_path = self._require_file(image, "镜像")
        if kind not in {"hash", "hashtree"}: raise ValueError("签名类型必须是 hash 或 hashtree")
        key_path = self._require_file(resolve_avb_key(key), "AVB 密钥") if key else None
        algorithm = algorithm or avb_algorithm_for_key(str(key_path) if key_path else None)
        if algorithm != "NONE" and key_path is None:
            raise ValueError("使用 RSA 签名算法时必须选择 AVB 密钥")
        if partition_size is not None and int(partition_size) <= 0:
            raise ValueError("分区大小必须大于零")
        if int(rollback_index) < 0:
            raise ValueError("回滚索引不能为负数")
        target = Path(output).expanduser().resolve() if output else image_path.with_name(image_path.stem + "_signed" + image_path.suffix)
        if target == image_path: raise ValueError("输出文件不能覆盖输入镜像，请指定新文件名")
        part = partition_name or image_path.stem
        command = "add_hash_footer" if kind == "hash" else "add_hashtree_footer"
        args = [command, "--image", target, "--partition_name", part, "--algorithm", algorithm]
        # Windows avbtool delegates FEC to the bundled avbroot implementation;
        # keep the default embedded FEC instead of silently disabling it.
        fec_args = []
        args.extend(fec_args)
        if partition_size is None:
            # Sparse files have a much larger logical size than their ZIP-like
            # container. AVB reserves must be calculated from that logical size.
            image_size = image_path.stat().st_size
            with image_path.open("rb") as source:
                header = source.read(28)
            if len(header) == 28 and struct.unpack_from("<I", header)[0] == 0xed26ff3a:
                block_size, blocks = struct.unpack_from("<II", header, 12)
                image_size = block_size * blocks
            if kind == "hash":
                help_result = self._tool_capture("avbtool", [command, "--help"])
                if help_result["ok"] and "--dynamic_partition_size" in help_result["output"]:
                    args.append("--dynamic_partition_size")
                else:
                    partition_size = ((image_size + 69632 + 4095) // 4096) * 4096
            else:
                partition_size = ((image_size + max(69632, image_size // 32) + 4095) // 4096) * 4096
                for _ in range(8):
                    sizing = self._tool_capture("avbtool", [command, "--partition_size", str(partition_size), "--calc_max_image_size", *fec_args])
                    numbers = [int(line.strip()) for line in sizing["output"].splitlines() if line.strip().isdigit()]
                    if not sizing["ok"] or not numbers or numbers[-1] <= 0:
                        raise RuntimeError(sizing["output"] or "无法计算 AVB 哈希树所需分区大小")
                    if numbers[-1] >= image_size:
                        break
                    partition_size += ((image_size - numbers[-1] + 69632 + 4095) // 4096) * 4096
                else:
                    raise RuntimeError("无法为 AVB 哈希树预留足够空间，请指定分区大小")
        if partition_size is not None:
            args.extend(["--partition_size", int(partition_size)])
        if key_path:
            args.extend(["--key", key_path])
            pass_file = key_path.parent / "passphrase.txt"
            if pass_file.is_file():
                args.extend(["--pass-file", pass_file])
        if kind == "hash": args.extend(["--rollback_index", int(rollback_index)])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".art-avb-", dir=target.parent) as stage_dir:
            staged = Path(stage_dir) / target.name
            shutil.copy2(image_path, staged)
            args[args.index("--image") + 1] = staged
            result = self._tool_capture("avbtool", args)
            if not result["ok"]:
                raise RuntimeError(result["output"] or f"avbtool 退出码 {result['code']}")
            if not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError("AVB 操作未生成有效镜像")
            os.replace(staged, target)
        result.update({"operation": "add_" + kind + "_footer", "image": str(image_path), "output": str(target)})
        return result

    def ota_status(self, project: str) -> dict:
        layout = self._layout(project)
        for directory in (layout.ota_work_dir, layout.ota_signkey_dir, layout.ota_stockzip_dir, layout.ota_inputimg_dir): directory.mkdir(parents=True, exist_ok=True)
        keys = ("avb.key", "ota.key", "avb_pkmd.bin", "ota.crt", "passphrase.txt")
        selected_file = layout.ota_stockzip_dir / ".select"
        selected = selected_file.read_text(encoding="utf-8").strip() if selected_file.is_file() else ""
        return {"path": str(layout.ota_work_dir), "selected": selected,
                "keys": {name: (layout.ota_signkey_dir / name).is_file() for name in keys},
                "stock_zips": [p.name for p in sorted(layout.ota_stockzip_dir.glob("*.zip")) if p.is_file()],
                "input_images": [p.name for p in sorted(layout.ota_inputimg_dir.glob("*.img")) if p.is_file()],
                "signed_zips": [p.name for p in sorted(layout.ota_work_dir.glob("*_signed.zip")) if p.is_file()]}

    def ota_select_zip(self, project: str, name: str) -> dict:
        layout = self._layout(project); candidate = Path(name).expanduser()
        if candidate.parent != Path("."):
            candidate = candidate.resolve()
            if candidate.parent != layout.ota_stockzip_dir.resolve(): raise ValueError("OTA 包必须位于 OTA_WORK/stock-zip")
        else: candidate = layout.ota_stockzip_dir / candidate.name
        if candidate.suffix.lower() != ".zip" or not candidate.is_file(): raise FileNotFoundError(f"OTA 包不存在：{candidate.name}")
        layout.ota_stockzip_dir.mkdir(parents=True, exist_ok=True); (layout.ota_stockzip_dir / ".select").write_text(candidate.name, encoding="utf-8")
        return self.ota_status(project)

    def ota_generate_keys(self, project: str, passphrase: str = "") -> dict:
        layout = self._layout(project); key_dir = layout.ota_signkey_dir; key_dir.mkdir(parents=True, exist_ok=True)
        pass_file = key_dir / "passphrase.txt"
        # An explicit empty file requests unencrypted keys without asking the
        # windowed GUI process to answer an interactive password prompt.
        pass_file.write_text(passphrase, encoding="utf-8")
        pf = ["--pass-file", pass_file]
        commands = [
            (["key", "generate-key", "-t", "rsa4096", "-o", key_dir / "avb.key"] + pf),
            (["key", "generate-key", "-t", "rsa4096", "-o", key_dir / "ota.key"] + pf),
            (["key", "encode-avb", "-k", key_dir / "avb.key", "-o", key_dir / "avb_pkmd.bin"] + pf),
            (["key", "generate-cert", "-k", key_dir / "ota.key", "-o", key_dir / "ota.crt"] + pf),]
        results = []
        try:
            for args in commands:
                result = self._tool_capture("avbroot", args, cwd=layout.ota_work_dir); results.append(result)
                if not result["ok"]: raise RuntimeError(result["output"] or f"avbroot 退出码 {result['code']}")
        finally:
            if not passphrase:
                pass_file.unlink(missing_ok=True)
        return {"results": results, **self.ota_status(project)}

    def ota_verify(self, project: str, archive: str | None = None) -> dict:
        layout = self._layout(project)
        if archive: zip_path = self._require_file(archive, "OTA 包")
        else:
            status = self.ota_status(project); selected = status["selected"]
            zip_path = self._require_file(layout.ota_stockzip_dir / selected, "OTA 包") if selected else None
            if zip_path is None:
                signed = sorted(layout.ota_work_dir.glob("*_signed.zip")); zip_path = signed[-1] if signed else None
            if zip_path is None: raise FileNotFoundError("没有可验证的 OTA 包")
        args = ["ota", "verify", "--input", zip_path]; cert = layout.ota_signkey_dir / "ota.crt"; pkmd = layout.ota_signkey_dir / "avb_pkmd.bin"
        if cert.is_file(): args.extend(["--cert-ota", cert])
        if pkmd.is_file(): args.extend(["--public-key-avb", pkmd])
        result = self._tool_capture("avbroot", args, cwd=layout.ota_work_dir); result.update({"archive": str(zip_path), "verified": result["ok"]}); return result

    def ota_patch(self, project: str, *, disable_avb: bool = False,
                  super_partitions: list[str] | None = None,
                  partition_sizes: dict[str, int | str] | None = None) -> dict:
        """Patch the selected stock OTA using images staged in input-img.

        ``super_partitions`` keeps compatibility with the original A.R.T
        command line, where ``--super-mode NAME`` marks an added partition as
        part of the dynamic super group.  The native command adapter translates
        that historical spelling to avbroot's ``--dynamic-partition`` flag.
        """
        super_partitions = [str(name).strip() for name in (super_partitions or []) if str(name).strip()]
        normalized_sizes = {}
        for name, value in (partition_sizes or {}).items():
            text = str(value).strip()
            try:
                size = int(text, 0)
            except ValueError:
                size = int(text, 10)
            if size <= 0:
                raise ValueError(f"分区 {name} 的大小必须大于 0")
            normalized_sizes[str(name).strip()] = size
        partition_sizes = normalized_sizes
        layout = self._layout(project); status = self.ota_status(project)
        if not status["selected"]: raise ValueError("请先在 OTA_WORK/stock-zip 中选择 OTA 包")
        zip_path = layout.ota_stockzip_dir / status["selected"]
        images = sorted(layout.ota_inputimg_dir.glob("*.img"))
        if not images: raise FileNotFoundError("OTA_WORK/input-img 内没有 .img 文件")
        key_dir = layout.ota_signkey_dir; ota_key = key_dir / "ota.key"; ota_crt = key_dir / "ota.crt"; avb_key = key_dir / "avb.key"
        if not ota_key.is_file() or not ota_crt.is_file(): raise FileNotFoundError("请先生成 OTA 密钥")
        help_result = self._tool_capture("avbroot", ["ota", "patch", "--help"], cwd=layout.ota_work_dir)
        if not help_result["ok"]:
            raise RuntimeError(help_result["output"] or "无法读取 avbroot 支持的 OTA 参数")
        help_text = help_result["output"]
        if disable_avb and "--disable-avb" not in help_text:
            raise ValueError("当前 Windows 原生 avbroot 不支持禁用 AVB 修补；请取消该选项，或配置支持 --disable-avb 的 avbroot 扩展版")
        if not disable_avb and not avb_key.is_file():
            raise FileNotFoundError("OTA 修补需要 avb.key，请先生成完整 OTA 密钥")
        listing = self._tool_capture("avbroot", ["ota", "list", "--input", zip_path], cwd=layout.ota_work_dir)
        if not listing["ok"]:
            raise RuntimeError(listing["output"] or "无法读取源 OTA 分区列表")
        existing = {line.strip() for line in listing["output"].splitlines() if line.strip()}
        image_args, added, staged_names = _ota_image_args(images, existing, partition_sizes)
        if added and "--add-partition" not in help_text:
            raise ValueError("当前 Windows 原生 avbroot 不支持添加 OTA 分区：" + "、".join(added) + "；请仅导入源 OTA 已有分区，或配置支持 --add-partition 的扩展版")
        output = layout.ota_work_dir / (zip_path.stem + "_signed.zip")
        args = ["ota", "patch", "--input", zip_path, "--output", output, "--key-ota", ota_key, "--cert-ota", ota_crt, "--rootless"]
        pass_file = key_dir / "passphrase.txt"
        if pass_file.is_file(): args.extend(["--pass-ota-file", pass_file])
        if not disable_avb and avb_key.is_file():
            args.extend(["--key-avb", avb_key])
            if pass_file.is_file(): args.extend(["--pass-avb-file", pass_file])
        args.extend(image_args)
        for part in super_partitions:
            if part not in staged_names:
                raise ValueError(f"super 分区 {part} 没有对应的 input-img 镜像")
            if part in existing:
                raise ValueError(f"super 分区 {part} 已存在于 OTA，不能作为新增动态分区标记")
            args.extend(["--super-mode", part])
        if disable_avb: args.extend(["--disable-avb", "--skip-system-ota-cert"])
        with tempfile.TemporaryDirectory(prefix=".art-ota-", dir=layout.ota_work_dir) as stage_dir:
            staged = Path(stage_dir) / output.name
            args[args.index("--output") + 1] = staged
            result = self._tool_capture("avbroot", args, cwd=layout.ota_work_dir)
            if not result["ok"]:
                raise RuntimeError(result["output"] or f"avbroot 退出码 {result['code']}")
            if not staged.is_file() or staged.stat().st_size == 0:
                raise RuntimeError("OTA 修补未生成有效 ZIP 文件")
            os.replace(staged, output)
        result.update({"archive": str(output), "disabled_avb": disable_avb,
                       "super_partitions": super_partitions,
                       "partition_sizes": partition_sizes})
        return result

    def job_request(self, operation: str, **params) -> dict:
        if operation not in {"extract", "repack", "repack_batch", "repack_super", "convert"}:
            raise ValueError("未知操作")
        if "project" in params:
            params["project"] = str(self._layout(params["project"]).project_dir)
        self.toolchain = detect_toolchain(self.root)
        return {"root": str(self.root), "operation": operation, "params": params,
                "backend": self.toolchain.mode}

    def start_job(self, request: dict):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["ART_BACKEND"] = request.get("backend", detect_toolchain(request["root"]).mode)
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
