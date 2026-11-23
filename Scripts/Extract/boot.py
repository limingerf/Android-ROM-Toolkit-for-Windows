"""Boot image unpacker for boot/vendor_boot images."""

import json
import os
import shutil
import stat
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


CPIO_MAGICS = (b"070701", b"070702", b"070707")
CPIO_HEADER_SIZE = 110
CPIO_ALIGN = 4
RAMDISK_METADATA_NAME = "ramdisk.meta.json"


def _is_cpio_archive(path):
    """True when *path* starts with a newc/odc cpio magic."""
    try:
        with open(path, 'rb') as stream:
            return stream.read(len(CPIO_MAGICS[0])) in CPIO_MAGICS
    except OSError:
        return False


def _cpio_padding(size):
    """Padding bytes that align the next cpio record to four bytes."""
    return (-size) % CPIO_ALIGN


def _read_cpio_entries(path):
    """Return ``{member: metadata}`` for a newc ramdisk, or ``None``.

    NTFS cannot represent the permission bits or symlinks a cpio archive
    declares.  The bundled Cygwin magiskboot turns a symlink into a reparse
    point Python cannot open and leaves every file at 0666, so a ramdisk
    unpacked on Windows loses ``/init``'s exec bit and the target of
    ``system/bin/snapuserd`` even though the tree looks complete.  Capture
    those values while the uncompressed archive is still available; the
    repacker writes them back into the new archive.
    """
    try:
        with open(path, 'rb') as stream:
            entries = {}
            while True:
                header = stream.read(CPIO_HEADER_SIZE)
                if len(header) < CPIO_HEADER_SIZE:
                    return None
                if header[:6] not in (b"070701", b"070702"):
                    return None
                values = []
                for index in range(13):
                    start = 6 + index * 8
                    try:
                        values.append(int(header[start:start + 8], 16))
                    except ValueError:
                        return None
                mode = values[1]
                uid = values[2]
                gid = values[3]
                filesize = values[6]
                rdevmajor = values[9]
                rdevminor = values[10]
                namesize = values[11]
                name = stream.read(namesize)
                if len(name) < namesize:
                    return None
                stream.seek(_cpio_padding(CPIO_HEADER_SIZE + namesize), 1)
                member = name.rstrip(b"\0").decode("utf-8", "surrogateescape")
                if member == "TRAILER!!!":
                    break
                data = stream.read(filesize)
                if len(data) < filesize:
                    return None
                stream.seek(_cpio_padding(filesize), 1)
                member = _normalise_cpio_member(member)
                if not member:
                    continue
                entries[member] = {
                    "mode": mode,
                    "uid": uid,
                    "gid": gid,
                    "rdev": [rdevmajor, rdevminor],
                    "link": (
                        data.decode("utf-8", "surrogateescape")
                        if stat.S_ISLNK(mode) else None
                    ),
                }
            return entries
    except OSError:
        return None


def _normalise_cpio_member(name):
    """Relative POSIX path used as the metadata key."""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return "" if name == "." else name


def _save_ramdisk_metadata(cpio, destination):
    """Record the cpio metadata beside the extracted ramdisk tree."""
    entries = _read_cpio_entries(cpio)
    if entries is None:
        return False
    try:
        with open(destination, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "entries": entries}, handle,
                      ensure_ascii=True, separators=(",", ":"))
    except OSError:
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

        # vendor_boot v4 allows a raw vendor ramdisk and some devices ship a
        # filesystem image there instead of a cpio archive.  magiskboot still
        # writes the payload as ramdisk.cpio, but there is no cpio tree to
        # extract; keep the bytes untouched so a later repack can reuse them.
        if not _is_cpio_archive(ramdisk):
            print(f"Ramdisk is not a cpio archive ({get_file_type(str(ramdisk))}), kept as {ramdisk.name}")
            return True

        if not _save_ramdisk_metadata(ramdisk, work_dir / RAMDISK_METADATA_NAME):
            print("Ramdisk metadata not recorded; repack will fall back to Windows permissions")

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
