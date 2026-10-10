"""Boot image repacker for boot/vendor_boot images."""

import os
import shlex
import subprocess

from Scripts.Platform.runtime import windows_to_wsl
from Scripts.Primary.Utils import V, BIN_PATH, call
from Scripts.Primary.Console import display
from Scripts.Primary.Utils import findfile

# A newc archive that holds no entry at all is exactly this long (trailer only).
EMPTY_CPIO_SIZE = 124


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
