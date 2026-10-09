"""Isolated ROM jobs with structured logs and staged output publication."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import hashlib
from pathlib import Path

from Scripts.application import ArtController, ROOT
from Scripts.Primary.WorkSpace import ProjectLayout, partition_name

def _emit(event, **payload):
    sys.__stdout__.write(json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n")
    sys.__stdout__.flush()

class _LogStream:
    def __init__(self):
        self.pending = ""
    def write(self, value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            _emit("log", message=line)
        return len(value)
    def flush(self):
        if self.pending:
            _emit("log", message=self.pending)
            self.pending = ""
    def isatty(self):
        return False
    @property
    def encoding(self):
        return "utf-8"
    @property
    def buffer(self):
        return self

def _bind(layout, sparse=False, root=None):
    from Scripts.Primary.Utils import V
    from Scripts.Primary.Settings import _SETUP_DEFAULTS
    from Scripts.Platform.runtime import detect_toolchain
    V.layout = layout
    V.project = layout.project_dir.name
    for key, value in {"project_dir": layout.project_dir, "input": layout.input_dir, "out": layout.out_dir,
                       "workspace": layout.workspace_dir, "config": layout.config_dir}.items():
        setattr(V, key, str(value) + os.sep)
    V.JM = True
    root = Path(root or ROOT).resolve()
    settings = root / "art-res" / "settings.json"
    V.SETUP_MANIFEST = dict(_SETUP_DEFAULTS)
    if settings.is_file():
        V.SETUP_MANIFEST.update(json.loads(settings.read_text(encoding="utf-8")))
    V.SETUP_MANIFEST["REPACK_SPARSE_IMG"] = "1" if sparse else "0"
    V.toolchain = detect_toolchain(root)

def _publish(pairs, *, replace=False):
    # Preflight all names before moving any staged artifact.
    for source, destination in pairs:
        if not source.exists():
            raise RuntimeError(f"任务未生成预期产物：{source}")
        if not replace and (destination.exists() or destination.is_symlink()):
            raise FileExistsError(f"产物已存在，未覆盖：{destination}")
    backup_dir = None
    backups = []
    if replace:
        # Extraction is intentionally repeatable.  Move old artifacts aside
        # first, then publish the complete staged result.  A failed publish
        # restores every old path so a working workspace is never lost.
        workspace = next((destination.parent for _, destination in pairs
                          if destination.name == "config" or destination.parent.name == "config"), None)
        if workspace is None:
            workspace = pairs[0][1].parent
        # Keep the transaction directory outside config/WORKSPACE.  Putting
        # it inside config makes a failed publish look like one of the files
        # being committed and can make rollback fail with FileNotFoundError.
        backup_dir = Path(tempfile.mkdtemp(prefix=".art-backup-", dir=workspace.parent))
        for index, (_source, destination) in enumerate(pairs):
            if destination.exists() or destination.is_symlink():
                backup = backup_dir / str(index)
                os.replace(destination, backup)
                backups.append((backup, destination))
    moved = []
    try:
        for source, destination in pairs:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() or destination.is_symlink():
                os.replace(destination, source)
        for backup, destination in reversed(backups):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, destination)
        if backup_dir:
            shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)
    return [str(destination) for _, destination in pairs]


def _tree_fingerprint(root):
    """Fingerprint a workspace tree without depending on filename encoding."""
    digest = hashlib.sha256()
    root = Path(root)
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8", "surrogatepass")
        stat = path.stat()
        digest.update(relative)
        digest.update(str(stat.st_size).encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_image(layout, partition):
    names = [partition]
    if partition.endswith(("_a", "_b")):
        names.append(partition[:-2])
    for name in names:
        for suffix in (".img", ".sparse.img", ".raw.img"):
            candidate = layout.input_dir / (name + suffix)
            if candidate.is_file():
                return candidate
    return None

def execute(request):
    controller = ArtController(request["root"])
    params = request["params"]
    layout = controller._layout(params["project"])
    operation = request["operation"]
    with tempfile.TemporaryDirectory(prefix=".art-job-", dir=layout.workspace_dir) as temporary:
        stage = ProjectLayout(Path(temporary) / "stage").initialize()
        _bind(stage, params.get("sparse", False), controller.root)
        if operation == "extract":
            return _extract(controller, layout, stage, params)
        if operation == "convert":
            return _convert(controller, layout, stage, params)
        if operation == "repack":
            return _repack(controller, layout, stage, params)
        if operation == "repack_batch":
            return _repack_batch(controller, layout, stage, params)
        if operation == "repack_super":
            return _repack_super(controller, layout, stage, params)
        raise ValueError("未知任务")

def _extract(controller, layout, stage, params):
    from Scripts.Primary.ImageTools import get_file_type
    from Scripts.Extract.image import decompress_img
    sources = params.get("sources")
    if sources is None:
        sources = [str(p) for p in layout.input_dir.iterdir() if p.is_file()
                   and not p.name.startswith(".") and not p.name.endswith((".transfer.list", ".patch.dat"))]
    if not sources:
        raise ValueError("INPUT 中没有可提取的文件")
    for index, value in enumerate(sources):
        source = Path(value).resolve()
        if source.parent != layout.input_dir.resolve() or not source.is_file():
            raise ValueError("先将输入导入该工程的 INPUT，再执行提取")
        fmt = controller.inspect_input(source)["format"]
        _emit("progress", current=index, total=len(sources), message=source.name)
        print(f"开始提取 {source.name} [{fmt}]", flush=True)
        if fmt == "payload":
            from Scripts.Extract.payload import info, main as unpack_payload, run as unpack_payload_part
            selected = params.get("payload_partitions") or []
            if selected:
                available = {name for name, _size in info(source)}
                unknown = sorted(set(selected) - available)
                if unknown:
                    raise ValueError(f"Payload 中不存在分区：{', '.join(unknown)}")
                for name in selected:
                    unpack_payload_part(str(source), str(stage.out_dir), name)
            else:
                unpack_payload(str(source), str(stage.out_dir))
            if params.get("deep", True):
                _dispatch_extracted_images(stage, source.name)
        elif fmt == "super":
            from Scripts.Primary.SuperTools import unpack
            unpack(str(source), str(stage.out_dir), parts=params.get("super_partitions") or None,
                   temp_dir=str(stage.workspace_dir))
            if params.get("deep", True):
                _dispatch_extracted_images(stage, source.name)
        elif fmt in {"ext", "erofs", "sparse", "boot", "vendor_boot"}:
            # Legacy EXT4 repairs its working image; keep INPUT immutable.
            name = partition_name(source)
            if (stage.workspace_dir / name).exists():
                raise ValueError(f"同一批任务包含重复分区：{name}")
            copied = stage.input_dir / source.name
            shutil.copyfile(source, copied)
            if not decompress_img(str(copied), str(stage.workspace_dir / name)):
                raise RuntimeError(f"{source.name} 解包失败，原工作区保持不变")
            # Keep an immutable baseline so a later repack can distinguish a
            # clean extraction from a workspace that the user edited.
            source_hash = _file_sha256(source)
            (stage.config_dir / f"{name}_source.json").write_text(json.dumps({
                "source_name": source.name,
                "source_sha256": source_hash,
                "workspace_sha256": _tree_fingerprint(stage.workspace_dir / name),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt in {"dat", "dat.br"}:
            from Scripts.Extract.dat_br import decompress_dat_batch
            copied = stage.input_dir / source.name
            shutil.copyfile(source, copied)
            prefix = source.name.split(".new.dat", 1)[0]
            companions = sorted(source.parent.glob(prefix + ".transfer.list"))
            for ancillary in companions:
                shutil.copyfile(ancillary, stage.input_dir / ancillary.name)
            if not companions:
                raise ValueError(f"{source.name} 缺少对应的 transfer.list")
            decompress_dat_batch([str(copied)], 2 if fmt == "dat.br" else 3)
            if not any(stage.config_dir.glob("*_fsconfig.txt")):
                raise RuntimeError("DAT 解包未生成分区 metadata")
        elif fmt == "win":
            from Scripts.Extract.win import decompress_win
            before = set(stage.workspace_dir.iterdir())
            decompress_win([str(source)])
            if set(stage.workspace_dir.iterdir()) == before:
                raise RuntimeError("WIN 解包未生成目录")
        else:
            raise ValueError(f"暂不支持自动提取 {source.name} [{fmt}]；ZIP 请先解压后导入")
    pairs = [(p, layout.out_dir / p.name) for p in stage.out_dir.iterdir()]
    pairs += [(p, layout.workspace_dir / p.name) for p in stage.workspace_dir.iterdir()
              if p.name != "config" and not p.name.startswith(".")]
    pairs += [(p, layout.config_dir / p.name) for p in stage.config_dir.iterdir()]
    if not pairs:
        raise RuntimeError("任务没有生成产物")
    outputs = _publish(pairs, replace=True)
    _emit("progress", current=len(sources), total=len(sources), message="提取完成")
    return {"project": str(layout.project_dir), "workspace": str(layout.workspace_dir), "outputs": outputs}


def _dispatch_extracted_images(stage, label):
    """Run the normal image dispatcher for payload/super output images."""
    from Scripts.Extract.image import decompress_img
    images = sorted(stage.out_dir.glob("*.img"))
    if not images:
        _emit("log", message=f"{label} 未生成可继续解包的 IMG，已保留原始输出")
        return
    for image in images:
        partition = partition_name(image)
        # Super unpackers may leave the temporary unsparsed super image in
        # OUT. It is already the container being processed; sending it back
        # through extract_super re-enters the old interactive confirmation.
        if partition == "super" or image.name.startswith(".super"):
            image.unlink(missing_ok=True)
            continue
        destination = stage.workspace_dir / partition
        _emit("progress", message=f"继续解包 {image.name}")
        if decompress_img(str(image), str(destination)):
            image.unlink(missing_ok=True)

def _convert(controller, layout, stage, params):
    from Scripts.Primary.ImageTools import raw_to_sparse, sparse_to_raw, get_file_type
    source = Path(params["source"]).resolve()
    if source.parent not in {layout.input_dir.resolve(), layout.out_dir.resolve()} or not source.is_file():
        raise ValueError("转换源必须在该工程 INPUT 或 OUT 中")
    target = params["target"]
    if target not in {"raw", "sparse"}:
        raise ValueError("转换目标必须是 raw 或 sparse")
    suffix = ".raw.img" if target == "raw" else ".sparse.img"
    destination = stage.out_dir / (source.stem + suffix)
    function = sparse_to_raw if target == "raw" else raw_to_sparse
    function(str(source), str(destination), temp_dir=str(stage.workspace_dir))
    return {"outputs": _publish([(destination, layout.out_dir / destination.name)], replace=True),
            "format": get_file_type(destination) if destination.exists() else target}

def _repack(controller, layout, stage, params):
    from Scripts.Primary.Utils import V
    partition = ProjectLayout.validate_component(params["partition"], "分区")
    source = layout.partition_dir(partition)
    if not source.is_dir():
        raise ValueError("分区目录不存在")
    original_format = next((p["format"] for p in controller.list_partitions(str(layout.project_dir)) if p["name"] == partition), "unknown")
    if original_format == "unknown":
        raise ValueError("缺少原始分区 metadata，不能确定回包格式")
    source_format = original_format
    output_format = params.get("filesystem", original_format)
    if source_format in {"boot", "vendor_boot"} and output_format not in {"auto", source_format}:
        raise ValueError("boot 镜像只能按原格式回包")
    if output_format not in {"ext", "erofs"}:
        output_format = original_format
    if source_format == "ext" and partition.removesuffix("_a").removesuffix("_b") in {"vendor", "odm"} and output_format != "ext":
        raise ValueError("vendor/odm 必须保持原始 EXT4 文件系统")
    if output_format == "ext":
        original_format = "ext"
    elif output_format == "erofs":
        original_format = "erofs"
    if original_format == "ext" and V.toolchain.mode == "native" and not V.toolchain.resolve("e2fsck", required=False):
        from Scripts.Platform.runtime import bootstrap_native_ext4_tools, detect_toolchain
        print("缺少 Windows 原生 EXT4 校验工具，正在补齐工具和 DLL…", flush=True)
        bootstrap_native_ext4_tools(controller.root, progress=lambda message: print(message, flush=True))
        V.toolchain = detect_toolchain(controller.root)
        V.toolchain.resolve("e2fsck")
    V.SETUP_MANIFEST["REPACK_IMAGE_SIZE"] = params.get("image_size", "auto")
    V.SETUP_MANIFEST.pop("REPACK_IMAGE_SIZE_BYTES", None)
    if V.SETUP_MANIFEST["REPACK_IMAGE_SIZE"] == "original":
        original_source = _source_image(layout, partition)
        if original_source:
            V.SETUP_MANIFEST["REPACK_IMAGE_SIZE_BYTES"] = str(original_source.stat().st_size)
            V.SETUP_MANIFEST["REPACK_IMAGE_SIZE"] = "auto"
        else:
            V.SETUP_MANIFEST["REPACK_IMAGE_SIZE"] = "auto"
    if params.get("erofs_compressor"):
        V.SETUP_MANIFEST["REPACK_EROFS_COMPRESSOR"] = params["erofs_compressor"]
    if params.get("erofs_level") is not None:
        V.SETUP_MANIFEST["REPACK_EROFS_LEVEL"] = str(params["erofs_level"])
    fixed_geometry = source_format == "ext" and output_format == "ext" and partition.removesuffix("_a").removesuffix("_b") in {"vendor", "odm"}
    source_image = _source_image(layout, partition) if fixed_geometry else None
    baseline = layout.config_dir / f"{partition}_source.json"
    clean_workspace = False
    if source_image and baseline.is_file():
        try:
            record = json.loads(baseline.read_text(encoding="utf-8"))
            clean_workspace = (
                record.get("source_sha256") == _file_sha256(source_image)
                and record.get("workspace_sha256") == _tree_fingerprint(source)
            )
        except (OSError, ValueError, json.JSONDecodeError):
            clean_workspace = False
    if fixed_geometry and params.get("target", "img") == "img" and source_image and (clean_workspace or not baseline.exists()):
        # A clean extraction must retain the original ext4 geometry.  Direct
        # passthrough also supports projects created by older releases that
        # predate the baseline manifest; edits in new extractions are checked.
        passthrough = stage.out_dir / (partition + ".img")
        shutil.copyfile(source_image, passthrough)
        from Scripts.Primary.ImageTools import is_sparse_image, sparse_to_raw
        check_image = passthrough
        if is_sparse_image(passthrough):
            check_image = Path(sparse_to_raw(passthrough, stage.workspace_dir / ".check.raw.img"))
        if V.toolchain.run(["e2fsck", "-fn", str(check_image)]) != 0:
            raise RuntimeError("原始 EXT4 文件系统检查未通过，未发布镜像")
        if params.get("_defer_publish"):
            return {"staged_artifacts": [str(passthrough)],
                    "format": "img", "validation": "原镜像直通，保留 vendor/odm 原始几何结构；e2fsck -fn 通过"}
        return {"outputs": _publish([(passthrough, layout.out_dir / passthrough.name)], replace=True),
                "format": "img", "validation": "原镜像直通，保留 vendor/odm 原始几何结构；e2fsck -fn 通过"}
    if fixed_geometry:
        raise ValueError("vendor/odm 工作区已修改，不能使用普通 EXT4 重建；请使用原位替换流程，或恢复为未修改状态")
    if original_format == "ext":
        for tool in ("mke2fs", "e2fsdroid", "e2fsck"):
            V.toolchain.resolve(tool)
    shutil.copytree(source, stage.workspace_dir / partition, symlinks=True)
    for meta in layout.config_dir.glob(partition + "_*"):
        shutil.copy2(meta, stage.config_dir / meta.name)
    copied = str(stage.workspace_dir / partition)
    fsconfig = str(stage.config_dir / (partition + "_fsconfig.txt"))
    contexts = str(stage.config_dir / (partition + "_contexts.txt"))
    target = params.get("target", "img")
    if target not in {"img", "dat", "dat.br"}:
        raise ValueError("不支持的回包格式")
    if original_format == "boot" and target != "img":
        raise ValueError("boot 只能回包为 IMG")
    flag = {"img": 8, "dat": 10, "dat.br": 11}[target]
    if original_format == "boot":
        from Scripts.ReMake.boot import boot_repack
        boot_repack(copied, str(stage.out_dir))
    else:
        if not Path(fsconfig).is_file() or not Path(contexts).is_file():
            raise ValueError("缺少 fsconfig 或 SELinux contexts")
        if original_format == "ext":
            from Scripts.ReMake.ext4 import recompress_ext4
            info_path = stage.config_dir / (partition + "_info.txt")
            recompress_ext4(copied, fsconfig, contexts, str(info_path) if info_path.is_file() else None, flag)
        else:
            from Scripts.ReMake.erofs import recompress_erofs
            recompress_erofs(copied, fsconfig, contexts, None, flag)
    artifacts = [p for p in stage.out_dir.iterdir() if p.is_file() and not p.name.endswith("_new.img")]
    if not artifacts:
        raise RuntimeError("回包失败，未生成产物")
    validation = "工具成功返回；尚未进行设备启动验证"
    image = stage.out_dir / (partition + ".img")
    if original_format == "ext" and image.is_file():
        from Scripts.Primary.ImageTools import is_sparse_image, sparse_to_raw
        check_image = image
        if is_sparse_image(image):
            check_image = Path(sparse_to_raw(image, stage.workspace_dir / ".check.raw.img"))
        if V.toolchain.run(["e2fsck", "-fn", str(check_image)]) != 0:
            raise RuntimeError("EXT4 文件系统检查未通过")
        validation = "e2fsck -fn 通过；尚未进行设备启动验证"
    if params.get("_defer_publish"):
        return {"staged_artifacts": [str(path) for path in artifacts], "format": target,
                "validation": validation}
    pairs = [(path, layout.out_dir / path.name) for path in artifacts]
    return {"outputs": _publish(pairs, replace=True), "format": target,
            "validation": validation}


def _repack_batch(controller, layout, stage, params):
    partitions = params.get("partitions") or []
    if not partitions:
        raise ValueError("请选择至少一个工作区分区")
    outputs = []
    validations = []
    collected = stage.workspace_dir / ".batch-repack-artifacts"
    collected.mkdir()
    for index, partition in enumerate(partitions):
        _emit("progress", current=index, total=len(partitions), message=f"回包 {partition}")
        result = _repack(controller, layout, stage,
                         {**params, "partition": partition, "_defer_publish": True})
        for artifact_name in result.get("staged_artifacts", []):
            artifact = Path(artifact_name)
            destination = collected / artifact.name
            if destination.exists():
                raise FileExistsError(f"批量回包产生重名产物：{destination.name}")
            os.replace(artifact, destination)
        if result.get("validation"):
            validations.append(f"{partition}: {result['validation']}")
    _emit("progress", current=len(partitions), total=len(partitions), message="批量回包完成")
    artifacts = sorted(path for path in collected.iterdir() if path.is_file())
    outputs = _publish([(path, layout.out_dir / path.name) for path in artifacts], replace=True)
    return {"outputs": outputs, "format": params.get("target", "img"),
            "validation": "；".join(validations)}


def _repack_super(controller, layout, stage, params):
    sources = params.get("sources") or []
    if not sources:
        raise ValueError("请选择至少一个 OUT/INPUT 镜像")
    super_type = int(params.get("super_type", 0))
    if super_type not in {0, 1, 2}:
        raise ValueError("super 类型必须是 0、1 或 2")
    copied = []
    allowed = {layout.input_dir.resolve(), layout.out_dir.resolve()}
    for value in sources:
        source = Path(value).resolve()
        if source.parent not in allowed or not source.is_file() or source.suffix.lower() != ".img":
            raise ValueError("super 输入必须是工程 INPUT 或 OUT 下的 IMG")
        destination = stage.input_dir / source.name
        shutil.copyfile(source, destination)
        # ``Path.stem`` leaves an extra suffix for ``system_a.sparse.img``
        # and ``system_a.raw.img``.  Use the canonical image-name parser so
        # sparse/raw variants are grouped into the same logical partition.
        logical_name = partition_name(source)
        if logical_name.endswith(("_a", "_b")):
            logical_name = logical_name[:-2]
        copied.append((logical_name, str(destination)))
    from Scripts.ReMake.super import repack_super
    repack_super(copied, super_type, 1 if params.get("sparse", False) else 0)
    artifacts = [p for p in stage.out_dir.iterdir() if p.is_file()]
    if not artifacts:
        raise RuntimeError("super 回包失败，未生成产物")
    return {"outputs": _publish([(path, layout.out_dir / path.name) for path in artifacts], replace=True),
            "format": "super", "super_type": super_type}

def main():
    log = _LogStream()
    try:
        request = json.loads(sys.stdin.readline())
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = execute(request)
        log.flush()
        _emit("result", data=result)
    except BaseException as error:
        log.flush()
        _emit("error", message=f"{type(error).__name__}: {error}")
        sys.exit(1)

if __name__ == "__main__":
    main()
