"""Boot image repacker for boot/vendor_boot images."""

import json
import os
import shlex
import stat
import subprocess

from Scripts.Platform.runtime import windows_to_wsl
from Scripts.Primary.Utils import V, BIN_PATH, call
from Scripts.Primary.Console import display
from Scripts.Primary.Utils import findfile

# A newc archive that holds no entry at all is exactly this long (trailer only).
EMPTY_CPIO_SIZE = 124
CPIO_HEADER_SIZE = 110
CPIO_ALIGN = 4
RAMDISK_METADATA_NAME = "ramdisk.meta.json"


# Locate the bundled busybox for the active backend.
def _busybox_path():
    """Return the busybox binary, which is `busybox.exe` on Windows."""
    for name in ("busybox.exe", "busybox"):
        found = findfile(name, BIN_PATH)
        if found:
            return found
    return None


# Collect the ramdisk tree as cpio member names.
def _ramdisk_entries(root):
    """Relative POSIX-style paths of everything inside a ramdisk tree."""
    entries = []
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            relative = os.path.relpath(os.path.join(current, name), root)
            entries.append(relative.replace(os.sep, "/"))
    entries.sort()
    return entries


# Pack the ramdisk tree into a newc cpio archive.
def _pack_ramdisk(ramdisk_dir, output_cpio):
    """Build a newc cpio archive from a ramdisk tree.

    `find` must not be used on the Windows backend: cmd.exe resolves it to the
    text-search utility, which rejects `-mindepth` and writes nothing, so the
    old pipeline produced a trailer-only archive that was then published as a
    successful repack.  The member list is generated here and piped into
    busybox cpio, and an empty archive is reported as a failure instead of
    replacing a working ramdisk with an unbootable one.
    """
    busybox = _busybox_path()
    if not busybox:
        print("Cannot Find busybox...")
        return False
    if os.path.isfile(output_cpio):
        os.remove(output_cpio)
    try:
        if V.toolchain.mode == "wsl":
            # GNU find exists inside WSL, so keep the pipeline there, but still
            # let busybox do the packing and run it from the ramdisk root.
            # Each path is converted and quoted on its own: windows_to_wsl()
            # rewrites one drive prefix per call, so leaving two Windows paths
            # in a single string left the output path unconverted (cpio then
            # could not create it inside WSL), and an unquoted path containing
            # a space was split into separate arguments by the shell.
            packed = shlex.quote(windows_to_wsl(busybox))
            target = shlex.quote(windows_to_wsl(output_cpio))
            command = f"find . -mindepth 1 | {packed} cpio -o -H newc -R 0:0 -F {target}"
            previous = os.getcwd()
            os.chdir(ramdisk_dir)
            try:
                status = V.toolchain.run(command, bundled=False, shell=True, output=True)
            finally:
                os.chdir(previous)
            if status != 0:
                print("cpio error: WSL 打包失败")
                return False
        else:
            entries = _ramdisk_entries(ramdisk_dir)
            if not entries:
                print("Ramdisk is empty...")
                return False
            process = subprocess.Popen(
                [busybox, "cpio", "-o", "-H", "newc", "-R", "0:0", "-F", output_cpio],
                cwd=ramdisk_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout, _ = process.communicate(
                ("\n".join(entries) + "\n").encode("utf-8"), timeout=120
            )
            if process.returncode != 0:
                detail = stdout.decode("utf-8", "replace").strip() if stdout else ""
                print(f"cpio error: {detail or process.returncode}")
                return False
    except (subprocess.TimeoutExpired, OSError) as error:
        print(f"cpio error: {error}")
        return False
    if not os.path.isfile(output_cpio) or os.path.getsize(output_cpio) <= EMPTY_CPIO_SIZE:
        print("Pack Ramdisk Fail... (empty cpio)")
        return False
    return True


def _cpio_padding(size):
    """Padding bytes that align the next cpio record to four bytes."""
    return (-size) % CPIO_ALIGN


def _newc_header(values):
    """Serialize the thirteen newc fields; busybox writes 070701 archives."""
    return b"070701" + b"".join(f"{int(value):08x}".encode("ascii") for value in values)


def _normalise_cpio_member(name):
    """Relative POSIX path used for metadata lookup."""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return "" if name == "." else name


# Reapply cpio metadata the Windows filesystem could not carry.
def _load_ramdisk_metadata(infile):
    """Read the metadata sidecar written by the extractor, if present."""
    path = os.path.join(infile, RAMDISK_METADATA_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"ramdisk metadata unreadable: {error}")
        return None
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else None


def _restore_ramdisk_metadata(cpio_path, entries):
    """Rewrite packed cpio records with the mode, owner and link metadata.

    busybox cpio can only see what NTFS exposes: every file is 0666 and a
    symlink extracted by the Cygwin magiskboot is an unreadable reparse point
    or a zero-byte placeholder.  The packed archive is therefore correct
    member-wise but unbootable (``/init`` is not executable and
    ``system/bin/snapuserd`` is empty).  Replace those fields from the sidecar
    captured at unpack time, rebuilding each record in place.
    """
    temporary = cpio_path + ".meta"
    try:
        seen = set()
        trailer = None
        with open(cpio_path, "rb") as source, open(temporary, "wb") as target:
            while True:
                header = source.read(CPIO_HEADER_SIZE)
                if not header:
                    raise ValueError("cpio archive has no trailer")
                if len(header) < CPIO_HEADER_SIZE or header[:6] != b"070701":
                    raise ValueError("unsupported cpio header")
                values = []
                for index in range(13):
                    start = 6 + index * 8
                    values.append(int(header[start:start + 8], 16))
                ino = values[0]
                mode = values[1]
                uid = values[2]
                gid = values[3]
                nlink = values[4]
                mtime = values[5]
                filesize = values[6]
                devmajor = values[7]
                devminor = values[8]
                namesize = values[11]
                name = source.read(namesize)
                if len(name) < namesize:
                    raise ValueError("truncated cpio name")
                name_padding = _cpio_padding(CPIO_HEADER_SIZE + namesize)
                name_pad = source.read(name_padding)
                if len(name_pad) < name_padding:
                    raise ValueError("truncated cpio padding")
                if name.rstrip(b"\0") == b"TRAILER!!!":
                    trailer = header + name + name_pad
                    break
                data = source.read(filesize)
                if len(data) < filesize:
                    raise ValueError("truncated cpio data")
                data_padding = _cpio_padding(filesize)
                data_pad = source.read(data_padding)
                if len(data_pad) < data_padding:
                    raise ValueError("truncated cpio data padding")

                member = _normalise_cpio_member(
                    name.rstrip(b"\0").decode("utf-8", "surrogateescape"))
                seen.add(member)
                metadata = entries.get(member)
                if not isinstance(metadata, dict):
                    target.write(header + name + name_pad + data + data_pad)
                    continue

                new_mode = int(metadata.get("mode", mode))
                new_uid = int(metadata.get("uid", uid))
                new_gid = int(metadata.get("gid", gid))
                rdev = metadata.get("rdev") or [0, 0]
                if stat.S_ISLNK(new_mode):
                    link = str(metadata.get("link") or "")
                    link_data = link.encode("utf-8", "surrogateescape")
                    new_values = [ino, new_mode, new_uid, new_gid, 1, mtime,
                                  len(link_data), devmajor, devminor, 0, 0,
                                  namesize, 0]
                    target.write(_newc_header(new_values) + name + name_pad
                                 + link_data + b"\0" * _cpio_padding(len(link_data)))
                else:
                    new_values = [ino, new_mode, new_uid, new_gid, nlink, mtime,
                                  filesize, devmajor, devminor, int(rdev[0]),
                                  int(rdev[1]), namesize, 0]
                    target.write(_newc_header(new_values) + name + name_pad
                                 + data + data_pad)

            # Windows cannot create character/block/fifo nodes, so the extractor
            # never puts them in the tree and busybox therefore drops them.  Put
            # back only those special members, and only when their parent
            # directory is still part of the tree; a deleted regular file must
            # stay deleted.
            for index, (member, metadata) in enumerate(entries.items()):
                if member in seen or not isinstance(metadata, dict):
                    continue
                new_mode = int(metadata.get("mode", 0))
                if not (stat.S_ISCHR(new_mode) or stat.S_ISBLK(new_mode)
                        or stat.S_ISFIFO(new_mode) or stat.S_ISSOCK(new_mode)):
                    continue
                parent = member.rpartition("/")[0]
                if parent and parent not in seen:
                    continue
                rdev = metadata.get("rdev") or [0, 0]
                name = member.encode("utf-8", "surrogateescape") + b"\0"
                new_values = [900000 + index, new_mode,
                              int(metadata.get("uid", 0)),
                              int(metadata.get("gid", 0)), 1, 0, 0, 0, 0,
                              int(rdev[0]), int(rdev[1]), len(name), 0]
                target.write(_newc_header(new_values) + name
                             + b"\0" * _cpio_padding(CPIO_HEADER_SIZE + len(name)))
            target.write(trailer)
        os.replace(temporary, cpio_path)
        return True
    except (OSError, ValueError, TypeError, IndexError) as error:
        print(f"cpio metadata restore failed: {error}")
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False


# Low-level ramdisk packing and magiskboot repack.
def dboot(infile, dist):
    or_dir = os.getcwd()
    if not os.path.exists(infile):
        print(f"Cannot Find {infile}...")
        return
    ramdisk_dir = os.path.join(infile, "ramdisk")
    if os.path.isdir(ramdisk_dir) and V.SETUP_MANIFEST.get('BOOT_SKIP_RAMDISK', '0') == '0':
        new_cpio = os.path.join(infile, "ramdisk-new.cpio")
        if not _pack_ramdisk(ramdisk_dir, new_cpio):
            print("Pack Ramdisk Fail... (cpio error)")
            os.chdir(or_dir)
            return
        metadata = _load_ramdisk_metadata(infile)
        if metadata is None:
            print("Ramdisk metadata not found; packed permissions may differ")
        elif not _restore_ramdisk_metadata(new_cpio, metadata):
            print("Pack Ramdisk Fail... (metadata restore)")
            os.chdir(or_dir)
            return
        print("Pack Ramdisk Successful..")
        try:
            os.remove(os.path.join(infile, "ramdisk.cpio"))
        except OSError:
            pass
        os.rename(new_cpio, os.path.join(infile, "ramdisk.cpio"))
    try:
        os.chdir(infile)
    except OSError as error:
        print("Ramdisk Not Found.. %s" % error)
        os.chdir(or_dir)
        return
    repack_args = ['magiskboot', 'repack', os.path.join(infile, "boot_o.img")]
    if call(repack_args) != 0:
        print("Pack boot Fail...")
        os.chdir(or_dir)
        return
    else:
        if os.path.exists(os.path.join(dist, os.path.basename(infile) + ".img")):
            os.remove(os.path.join(dist, os.path.basename(infile) + ".img"))
        os.rename(infile + os.sep + "new-boot.img", os.path.join(dist, os.path.basename(infile) + ".img"))
        os.chdir(or_dir)
        print("Pack Successful...")


# Public boot/vendor_boot repack entry point.
def boot_repack(source, distance):
    """Entry point for boot repack."""
    if not os.path.isdir(distance):
        os.makedirs(distance)
    display(f"重新合成: {os.path.basename(source)}.img")
    return dboot(source, distance)
