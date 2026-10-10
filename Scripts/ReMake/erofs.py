"""EROFS image repacker."""

import os
import shutil
import time

from Scripts.Primary.Utils import (
    CLOSE,
    RED,
    V,
    call,
    get_dir_size,
)
from Scripts.Primary.FileConfigPatcher import patch_fsconfig, restore_symlinks
from Scripts.Primary.Console import display
from Scripts.Primary.WorkSpace import load_image_json
from Scripts.ReMake.dat_br import recompress_dat_br
from Scripts.ReMake.metadata import normalize_metadata


# Metadata normalization, image construction, and DAT hand-off.
def walk_contexts(path):
    """Normalize generated metadata without changing rule order."""
    return normalize_metadata(path)


# Restore symlinks and refuse to build an image that silently lost them.
def _ensure_symlinks(source, fsconfig):
    """Recreate symlinks the extraction could not create on Windows.

    `mkfs.erofs` derives the inode type from the source tree, so a workspace
    extracted without the symbolic-link privilege would repack /bin, /etc and
    every other symlink as a regular file, which cannot boot.
    """
    report = restore_symlinks(source, fsconfig)
    if report["restored"] or report["failed"] or report["skipped"]:
        display(
            f"符号链接: 恢复 {report['restored']}，已存在 {report['present']}，"
            f"跳过 {len(report['skipped'])}，失败 {len(report['failed'])}"
        )
    if not report["failed"]:
        return
    if V.SETUP_MANIFEST.get("ALLOW_SYMLINK_LOSS", "0") == "1":
        display(
            f"{RED}警告: {len(report['failed'])} 个符号链接未能恢复，"
            f"这些路径会被回包成普通文件{CLOSE}"
        )
        return
    preview = "；".join(report["failed"][:5])
    raise RuntimeError(
        f"{len(report['failed'])} 个符号链接无法在工作区恢复（{preview}）。"
        "mkfs.erofs 只依据源目录的文件类型生成 inode，继续回包会把 /bin、/etc、/init 等"
        "符号链接写成普通文件，刷入后无法开机。"
        "请开启 Windows 开发者模式（或以管理员身份运行）后重新解包，或改用 WSL 后端；"
        "确需保留当前结果请设置 ALLOW_SYMLINK_LOSS=1。"
    )


# Prepare sizes, timestamps, metadata, and output paths.
def _prepare(source, fsconfig, contexts, dumpinfo):
    label = os.path.basename(source)
    os.makedirs(V.out, exist_ok=True)
    distance = os.path.join(V.out, f"{label}.img")
    if os.path.isfile(distance):
        os.remove(distance)

    walk_contexts(fsconfig)
    patch_fsconfig(source, fsconfig)
    walk_contexts(fsconfig)
    walk_contexts(contexts)
    _ensure_symlinks(source, fsconfig)

    timestamp = (
        int(time.time())
        if V.SETUP_MANIFEST["UTC"].lower() == "live"
        else V.SETUP_MANIFEST["UTC"]
    )
    if dumpinfo:
        _fsize, dsize, _inodes, _block_size, _blocks, _per_group, _mount_point = (
            load_image_json(dumpinfo, source)
        )
        size = dsize
    else:
        size = get_dir_size(source, 1.3)
        if int(size) <= 1048576:
            size = 1048576

    requested_bytes = V.SETUP_MANIFEST.get("REPACK_IMAGE_SIZE_BYTES")
    requested_size = V.SETUP_MANIFEST.get("REPACK_IMAGE_SIZE", "auto")
    if requested_bytes:
        try:
            size = max(1048576, int(requested_bytes))
        except (TypeError, ValueError):
            pass
    elif str(requested_size).lower() not in {"", "auto", "automatic"}:
        try:
            size = max(1048576, int(float(requested_size)) * 1024 * 1024)
        except (TypeError, ValueError):
            pass

    new_distance = os.path.join(V.out, f"{label}_new.img")
    if os.path.isfile(new_distance):
        os.remove(new_distance)
    return {
        "label": label,
        "distance": distance,
        "new_distance": new_distance,
        "timestamp": timestamp,
        "size": size,
    }


# Build EROFS and optionally convert to sparse/DAT.
def _write_image(state, fsconfig, contexts, source, flag):
    label = state["label"]
    distance = state["distance"]
    new_distance = state["new_distance"]
    level = V.SETUP_MANIFEST.get("REPACK_EROFS_LEVEL", V.SETUP_MANIFEST.get("EROFS_LEVEL", "1"))
    erofs_format = V.SETUP_MANIFEST.get("REPACK_EROFS_COMPRESSOR")
    if not erofs_format:
        # The setting is 0 = 无压缩 / 1 = LZ4HC / 2 = LZ4 (see Settings and the
        # Qt repack dialog).  Anything other than "1" used to fall through to
        # lz4, so "no compression" silently produced an lz4 image.
        erofs_format = {"0": "", "1": "lz4hc"}.get(str(V.SETUP_MANIFEST["RESIZE_EROFSIMG"]), "lz4")
    erofs_compress = (
        f"{erofs_format},{level}" if erofs_format not in ("", "lz4") else erofs_format
    )
    mkerofs_cmd = ["mkfs.erofs"]
    if V.SETUP_MANIFEST.get("EROFS_OLD_KERNEL", "0") == "1":
        # erofs-utils >= 1.6 removed the pre-5.4 layout and only warns, so say
        # plainly that the compatibility request has no effect.  Measured with
        # the bundled mkfs.erofs 1.9.3: -E legacy-compress produced a byte-count
        # identical to plain -zlz4hc.
        print("> 提示: 当前 mkfs.erofs 已移除 <Linux 5.4 的旧布局，"
              "「EROFS 旧内核兼容」设置不会生效")
        mkerofs_cmd.extend(["-E", "legacy-compress"])
    if erofs_compress:
        # Omitting -z is how mkfs.erofs builds an uncompressed image.
        mkerofs_cmd.append(f"-z{erofs_compress}")
    mkerofs_cmd.extend(
        [
            "-T",
            str(state["timestamp"]),
            f"--mount-point=/{label}",
            f"--product-out={V.workspace}",
            f"--fs-config-file={fsconfig}",
            f"--file-contexts={contexts}",
            new_distance,
            source,
        ]
    )

    if call(mkerofs_cmd) != 0:
        try:
            os.remove(new_distance)
        except OSError:
            pass
    if not os.path.isfile(new_distance):
        return False

    print(" Done")
    # A sparse file is far smaller than the image it describes, because
    # img2simg keeps only the non-zero chunks.  Capture the real image size
    # before any format conversion (and before `new_distance` is removed), so
    # the dynamic-partition op list and any later super composition see the
    # truth rather than the sparse file's byte count.
    state["raw_size"] = os.path.getsize(new_distance)
    if V.SETUP_MANIFEST["REPACK_SPARSE_IMG"] == "1" or flag > 9:
        display("开始转换: sparse format ...")
        if call(["img2simg", new_distance, distance]) != 0:
            try:
                os.remove(new_distance)
            except OSError:
                pass
            return False
        try:
            os.remove(new_distance)
        except OSError:
            pass
    else:
        if os.path.isfile(distance):
            os.remove(distance)
        os.rename(new_distance, distance)
    return True


# Update dynamic-partition operation-list sizes.
def _update_dynamic_partitions(label, distance, image_size=None):
    if not os.path.isfile(distance):
        print(f" {RED}打包失败{CLOSE}")
        return

    op_list = os.path.join(V.input, "dynamic_partitions_op_list")
    new_op_list = os.path.join(V.out, "dynamic_partitions_op_list")
    if os.path.isfile(op_list) or os.path.isfile(new_op_list):
        if not os.path.isfile(new_op_list):
            shutil.copyfile(op_list, new_op_list)
    else:
        # Honour the configured group name instead of assuming Qualcomm's.
        group_name = V.SETUP_MANIFEST.get('GROUP_NAME') or 'qti_dynamic_partitions'
        content = "remove_all_groups\n"
        for slot in ("_a", "_b"):
            content += (
                f"add_group {group_name}{slot} "
                f"{V.SETUP_MANIFEST['SUPER_SIZE']}\n"
            )
        for partition in ("system", "system_ext", "product", "vendor", "odm"):
            for slot in ("_a", "_b"):
                content += f"add {partition}{slot} {group_name}{slot}\n"
        # Deliberately no `resize` placeholders: only the partition built by
        # this job has a known size, and the old `resize <part> 2` lines would
        # shrink every other logical partition to two bytes when the list is used.
        with open(new_op_list, "w", encoding="UTF-8", newline="\n") as target:
            target.write(content)

    # Prefer the caller's real image size.  `img2simg` output is a sparse file
    # whose byte count is unrelated to the partition it stands for, so measuring
    # `distance` would resize the partition far below the image that gets flashed.
    renew_size = int(image_size) if image_size else os.path.getsize(distance)
    with open(new_op_list, "r", encoding="UTF-8") as source:
        lines = source.readlines()
    resized = False
    with open(new_op_list, "w", encoding="UTF-8") as target:
        for line in lines:
            if f"resize {label} " in line:
                line = f"resize {label} {renew_size}\n"
                resized = True
            elif f"resize {label}_a " in line:
                line = f"resize {label}_a {renew_size}\n"
                resized = True
            target.write(line)
        if not resized:
            suffix = label if label.endswith(("_a", "_b")) else label + "_a"
            target.write(f"resize {suffix} {renew_size}\n")

    return True


# Public EROFS repack entry point.
def recompress_erofs(source, fsconfig, contexts, dumpinfo, flag=8):
    """Recompress a partition directory into an EROFS image or DAT package."""
    state = _prepare(source, fsconfig, contexts, dumpinfo)
    printinform = (
        f"Size:{state['size']}|FsT:erofs|FsR:ro|"
        f"Sparse:{V.SETUP_MANIFEST['REPACK_SPARSE_IMG']}"
    )
    if V.SETUP_MANIFEST["RESIZE_EROFSIMG"] == "1":
        printinform += "|lz4hc"
    elif V.SETUP_MANIFEST["RESIZE_EROFSIMG"] == "2":
        printinform += "|lz4"
    display(printinform)
    display(f"重新合成: {state['label']}.img ...", 4)
    if _write_image(state, fsconfig, contexts, source, flag):
        if _update_dynamic_partitions(
            state["label"], state["distance"], state.get("raw_size")
        ) and flag > 9:
            recompress_dat_br(state["label"], state["distance"], flag)
