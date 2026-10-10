"""Boot image unpacker for boot/vendor_boot images."""

import os
import shutil
from pathlib import Path

from Scripts.Primary.Utils import V, call, rmdire
from Scripts.Primary.Console import display
from Scripts.Primary.ImageTools import get_file_type


# The bundled magiskboot rejects zstd ("Unknown compression method: [zstd]"), so
# such a ramdisk is expanded with Python instead.  The repack then writes the
# ramdisk back as gzip, which magiskboot can produce.
def _repack_compression(comp):
    """Compression magiskboot should write back for a detected ramdisk format."""
    return 'gzip' if comp == 'zstd' else comp


def _decompress_zstd(source, destination):
    """Expand a zstd stream without magiskboot.

    ``copy_stream`` reports a truncated frame as a normal end of stream, which
    would turn a damaged ramdisk into an empty file, so the frame end is checked
    explicitly and a partial output is removed.
    """
    try:
        import zstandard
    except ImportError:
        print("缺少 zstandard 模块，无法解压 zstd ramdisk")
        return False
    try:
        with open(source, 'rb') as compressed, open(destination, 'wb') as expanded:
            decompressor = zstandard.ZstdDecompressor().decompressobj()
            for chunk in iter(lambda: compressed.read(1024 * 1024), b''):
                expanded.write(decompressor.decompress(chunk))
            if not decompressor.eof:
                raise zstandard.ZstdError('zstd 数据在帧结束前结束')
    except (OSError, zstandard.ZstdError) as error:
        print(f"zstd 解压失败: {error}")
        try:
            os.remove(destination)
        except OSError:
            pass
        return False
    return True


def _decompress_ramdisk(compressed, destination, comp):
    """Expand *compressed* into *destination* according to the detected format."""
    if comp == 'zstd':
        return _decompress_zstd(compressed, destination)
    return call(['magiskboot', 'decompress', str(compressed), str(destination)]) == 0


# Low-level magiskboot unpack and ramdisk extraction.
def unpackboot(file, distance):
    """Unpack a boot image into a staging directory and report success."""
    original_dir = os.getcwd()
    work_dir = Path(distance)
    try:
        rmdire(work_dir)
        work_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(file, work_dir / "boot_o.img")
        os.chdir(work_dir)
        if call(['magiskboot', 'unpack', '-h', str(file)]) != 0:
            print(f"Unpack {file} Fail...")
            return False

        ramdisk = work_dir / 'ramdisk.cpio'
        if not ramdisk.is_file() or V.SETUP_MANIFEST.get('BOOT_SKIP_RAMDISK', '0') == '1':
            print("Unpack Done!")
            return True

        comp = get_file_type(str(ramdisk))
        print(f"Ramdisk is {comp}")
        (work_dir / 'comp').write_text(_repack_compression(comp), encoding='utf-8')
        if comp != 'unknown':
            compressed_ramdisk = work_dir / 'ramdisk.cpio.comp'
            os.replace(ramdisk, compressed_ramdisk)
            if not _decompress_ramdisk(compressed_ramdisk, ramdisk, comp):
                print("Decompress Ramdisk Fail...")
                return False

        ramdisk_dir = work_dir / 'ramdisk'
        ramdisk_dir.mkdir(exist_ok=True)
        print("Unpacking Ramdisk...")
        os.chdir(ramdisk_dir)
        if call(['magiskboot', 'cpio', str(work_dir / 'ramdisk.cpio'), 'extract']) != 0:
            print("Unpack Ramdisk Fail...")
            os.chdir(work_dir)
            return False
        os.chdir(work_dir)
        return True
    except OSError as error:
        print(f"Unpack {file} Fail: {error}")
        return False
    finally:
        os.chdir(original_dir)


# Public boot/vendor_boot extraction entry point.
def boot_unpack(source, distance):
    """Entry point for boot unpack."""
    if not os.path.isdir(distance):
        os.makedirs(distance)
    display(f"正在分解: {os.path.basename(source)}")
    return unpackboot(source, distance)
