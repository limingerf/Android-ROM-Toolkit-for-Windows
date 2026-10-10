"""Image extraction entry point and dispatcher — routes to format-specific extractors.

Format-specific logic lives in:
- Scripts.Extract.payload : payload.bin
- Scripts.Extract.dat_br     : new.dat / new.dat.br
- Scripts.Extract.ext4    : EXT4 / sparse images
- Scripts.Extract.erofs   : EROFS images
- Scripts.Extract.super   : super.img
- Scripts.Extract.boot    : boot / vendor_boot images
- Scripts.Extract.win     : .win archives
"""

import os
import json
import shutil
import struct
import tempfile
import time
from glob import glob
from pathlib import Path

from Scripts.Primary.Utils import V, RED, CLOSE, remove_tree
from Scripts.Primary.Console import display
from Scripts.Primary.ImageTools import get_file_type
from Scripts.Primary.WorkSpace import LayoutError
from Scripts.Primary.WorkSpace import (
    partition_name, workspace_partition, workspace_temp,
    _destination_partition, _stage_work_source, envelop_project,
)


def _fast_ext4_extract(source, partition, destination):
    """Use the bundled native imgkit extractor when available.

    The embedded Python reader remains the compatibility fallback, but a
    native extraction avoids walking every file through Python on Windows and
    is substantially faster for multi-gigabyte system images.
    """
    tool = V.toolchain.resolve('imgkit', required=False)
    if not tool or not os.name == 'nt':
        return False
    workspace = Path(destination).resolve().parent
    work_root = Path(tempfile.mkdtemp(prefix=f'.{partition}.imgkit-', dir=workspace))
    config = work_root / 'config'
    try:
        result = V.toolchain.run([
            'imgkit', 'unpack', '-i', str(Path(source).resolve()),
            '-o', str(work_root), '--clean', '-l', '1',
        ])
        output = work_root / partition
        raw_contexts = config / f'{partition}_file_contexts'
        raw_fsconfig = config / f'{partition}_fs_config'
        if result != 0 or not output.is_dir() or not raw_contexts.is_file() or not raw_fsconfig.is_file():
            return False
        contexts = config / f'{partition}_contexts.txt'
        fsconfig = config / f'{partition}_fsconfig.txt'
        os.replace(raw_contexts, contexts)
        os.replace(raw_fsconfig, fsconfig)
        # Generate the compact image manifest consumed by the existing
        # repacker.  imgkit already emitted the authoritative fs metadata.
        with open(source, 'rb') as stream:
            stream.seek(1024)
            superblock = stream.read(1024)
        if len(superblock) < 136 or struct.unpack_from('<H', superblock, 0x38)[0] != 0xEF53:
            return False
        inode_count = struct.unpack_from('<I', superblock, 0)[0]
        block_size = 1024 << struct.unpack_from('<I', superblock, 0x18)[0]
        # 'c' is s_blocks_per_group (offset 32), matching the pure Python
        # extractor.  It used to be read from 0x28, which is
        # s_inodes_per_group, so the two writers disagreed on the same key.
        per_group = struct.unpack_from('<I', superblock, 0x20)[0]
        label = bytes(superblock[0x78:0x88]).rstrip(b'\0').decode('utf-8', 'replace')
        (config / f'{partition}_info.txt').write_text(json.dumps({
            'a': inode_count, 'b': block_size, 'c': per_group,
            'd': label or partition, 'e': 'ext4', 's': Path(source).stat().st_size,
        }, indent=4), encoding='utf-8')
        (config / f'{partition}_space.txt').write_text('', encoding='utf-8')
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                remove_tree(destination)
            else:
                destination.unlink()
        os.replace(output, destination)
        target_config = workspace / 'config'
        target_config.mkdir(parents=True, exist_ok=True)
        for metadata in config.iterdir():
            os.replace(metadata, target_config / metadata.name)
        return True
    except (OSError, ValueError, struct.error, UnicodeError):
        return False
    finally:
        remove_tree(work_root, ignore_errors=True)


# Single-image format dispatcher.
def decompress_img(source, distance=None, keep=1):
    """Extract one image directly into WORKSPACE/<partition>/.

    Dispatches to the appropriate format-specific extractor based on
    the image type detected by get_file_type.
    """
    del keep
    source_type = get_file_type(source)
    if source_type not in ('boot', 'vendor_boot', 'sparse', 'ext', 'erofs', 'super'):
        print(f'> 不支持的镜像类型: {source_type}')
        return False
    if os.path.basename(source) in ('dsp.img', 'exaid.img', 'cust.img'):
        # Deliberate skip, not a failure: the worker aborts the whole job when
        # this dispatcher returns a falsy value.
        print(f'> 跳过不支持的镜像: {os.path.basename(source)}')
        return True

    try:
        working_source = _stage_work_source(source, 'image')
        partition = _destination_partition(distance, working_source)
    except (LayoutError, OSError) as error:
        print(f'> 无法准备镜像: {error}')
        return

    destination = workspace_partition(partition)
    s_time = time.time()
    file_type = get_file_type(working_source)
    committed = False

    if file_type in ('boot', 'vendor_boot'):
        from Scripts.Extract.boot import boot_unpack
        from Scripts.Primary.WorkSpace import create_partition_stage, metadata_path, _commit_extracted_partition
        try:
            _, staged_partition, staged_config = create_partition_stage(partition, 'boot-extract')
            if not boot_unpack(working_source, str(staged_partition)):
                raise LayoutError(f'{partition} boot 解包失败')
            if not os.path.isfile(os.path.join(staged_partition, 'boot_o.img')):
                raise LayoutError(f'{partition} boot 解包未生成 boot_o.img')
            metadata_path(staged_config, partition, '_kernel.txt').touch()
            committed = _commit_extracted_partition(
                partition, staged_partition, {f'{partition}_kernel.txt'})
        except (LayoutError, OSError) as error:
            print(f'> {partition} boot 分解失败: {error}')

    elif file_type == 'sparse':
        from Scripts.Primary.ImageTools import sparse_to_raw
        raw_source = os.path.join(V.workspace, f'.{partition}.unsparse.img')
        try:
            sparse_to_raw(working_source, raw_source, temp_dir=V.workspace)
            return decompress_img(raw_source, destination)
        except (OSError, ValueError) as error:
            print(f'> Sparse 转换失败: {error}')
        finally:
            if os.path.isfile(raw_source):
                os.remove(raw_source)
        return False

    elif file_type == 'ext':
        from Scripts.Extract.ext4 import extract_ext4
        committed = _fast_ext4_extract(working_source, partition, destination)
        if not committed:
            committed = extract_ext4(working_source, partition, destination)

    elif file_type == 'erofs':
        from Scripts.Extract.erofs import extract_erofs
        committed = extract_erofs(working_source, partition, destination)

    elif file_type == 'super':
        from Scripts.Extract.super import extract_super
        return extract_super(working_source, partition)

    if committed:
        print('\x1b[1;32m %ds Done\x1b[0m' % (time.time() - s_time))
    elif file_type not in ('boot', 'vendor_boot'):
        from rich import print as echo
        echo('[red][Failed][/]')
    return bool(committed)


# Batch dispatcher for DAT.BR, DAT, and IMG menu options.
def decompress(infile, flag=4):
    """Batch extraction entry point for dat.br / dat / img files."""
    if flag in (2, 3):
        from Scripts.Extract.dat_br import decompress_dat_batch
        decompress_dat_batch(infile, flag)
        return

    # flag 4 (img): sequential with per-file confirmation
    for part in sorted(infile):
        if not os.path.isfile(part):
            continue
        try:
            if os.path.basename(part) in ('dsp.img', 'cust.img'):
                continue
            if get_file_type(part) not in ('ext', 'sparse', 'erofs', 'super', 'boot', 'vendor_boot'):
                continue
            if not V.JM:
                display(f'是否分解: {os.path.basename(part)} [1/0]: ', 2, '')
                if input() != '1':
                    continue
            decompress_img(part, workspace_partition(partition_name(part)))
        except LayoutError as error:
            print(f'> 跳过 {os.path.basename(part)}: {error}')


# ROM ZIP import and plugin installation workflow.
def extract_zrom(rom):
    """Extract a ROM zip or install a plugin."""
    import zipfile
    import shutil
    from Scripts.Primary.Utils import rmdire, safe_extract_zip, change_permissions_recursive

    MOD_DIR = os.getcwd() + os.sep + "local/sub/"

    if not zipfile.is_zipfile(rom):
        input('> 破损的zip或不支持的zip类型')
        return

    with zipfile.ZipFile(rom) as archive:
        zip_lists = archive.namelist()
        if 'run.sh' in zip_lists:
            if not os.path.isdir(MOD_DIR):
                os.makedirs(MOD_DIR)
            mod_name = os.path.basename(rom).rsplit('.', 1)[0].replace(' ', '_')
            sub_dir = MOD_DIR + 'DNA_' + mod_name
            if not os.path.isdir(sub_dir):
                display(f'是否安装插件: {mod_name} ? [1/0]: ', 2, '')
            else:
                display(f'已安装插件: {mod_name}，是否删除原插件后安装 ? [0/1]: ', 2, '')
            if input() == '1':
                rmdire(sub_dir)
                os.makedirs(sub_dir, exist_ok=True)
                try:
                    safe_extract_zip(archive, sub_dir)
                except LayoutError as error:
                    rmdire(sub_dir)
                    input(f'> 插件安装失败: {error}')
                    return
                if os.path.isfile(sub_dir + os.sep + 'run.sh'):
                    change_permissions_recursive(sub_dir, 0o777)
                    print('\x1b[1;31m\n 安装完成 !!!\x1b[0m')
                else:
                    rmdire(sub_dir)
                    print('\x1b[1;31m\n 安装失败 !!!\x1b[0m')
            return

        V.project = 'DNA_' + os.path.basename(rom).rsplit('.', 1)[0]
        try:
            envelop_project()
        except (LayoutError, OSError, ValueError) as error:
            input(f'> 无法创建或打开工程: {error}')
            return

        import_dir = workspace_temp('import')
        print(f'> 解压缩: {os.path.basename(rom)} 到 WORKSPACE')
        try:
            safe_extract_zip(archive, import_dir)
        except LayoutError as error:
            input(f'> ROM ZIP 解压失败: {error}')
            return

    payload_files = sorted(glob(os.path.join(import_dir, '**', 'payload.bin'), recursive=True))
    if payload_files:
        from Scripts.Extract.payload import decompress_bin
        decompress_bin(
            payload_files[0],
            flag=input(f'> {RED}选择提取方式:  [0]全盘提取  [1]指定镜像{CLOSE} >> '),
        )
        remove_tree(import_dir, ignore_errors=True)
        return

    dat_br_files = sorted(glob(os.path.join(import_dir, '**', '*.new.dat.br'), recursive=True))
    dat_files = sorted(glob(os.path.join(import_dir, '**', '*.new.dat'), recursive=True))
    img_files = sorted(glob(os.path.join(import_dir, '**', '*.img'), recursive=True))

    if dat_br_files:
        infile, able = dat_br_files, 2
    elif dat_files:
        infile, able = dat_files, 3
    elif img_files:
        infile, able = img_files, 4
    else:
        input('> 仅支持含有payload.bin/*.new.dat/*.new.dat.br/*.img的zip固件')
        remove_tree(import_dir, ignore_errors=True)
        return

    decompress(infile, able)
    remove_tree(import_dir, ignore_errors=True)
