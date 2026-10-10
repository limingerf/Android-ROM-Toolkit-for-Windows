"""Android fsconfig scanning and metadata patching."""

import os
import stat
from collections import deque

# ---------------------------------------------------------------------------
# fsconfig scanning and metadata completion for image repacking
# ---------------------------------------------------------------------------
# Read the existing fsconfig into a path -> metadata mapping.
def scanfs(file: str) -> dict:
    """
    Scan Origin File , Return A dict
    :param file:
    :return:
    """
    filesystem_config = {}
    with open(file, "r", encoding='utf-8', errors='surrogateescape') as file_:
        for i in file_.readlines():
            if not i.strip():
                print("[W] data is empty!")
                continue
            try:
                filepath, *other = i.strip().split()
            except (TypeError,) as e:
                print(f'[W] Skip {i} {e}')
                continue
            filesystem_config[filepath] = other
            if (long := len(other)) > 4:
                print(f"[W] {i[0]} has too much data-{long}.")
    return filesystem_config


# Enumerate directories, files, and required root entries.
def scan_dir(folder: str) -> list:
    """
    Scan Folder , Return A path One By One
    :param folder:
    :return:
    """
    allfiles = ['/', '/lost+found']
    yield os.path.basename(folder)
    for root, dirs, files in os.walk(folder, topdown=True):
        for dir_ in dirs:
            yield os.path.join(root, dir_).replace(folder, os.path.basename(folder)).replace('\\', '/')
        for file in files:
            yield os.path.join(root, file).replace(folder, os.path.basename(folder)).replace('\\', '/')
        yield from allfiles


def islink(file) -> str:
    """
    Determine if it is a SymLink
    :param file:
    :return:
    """
    if os.path.islink(file):
        return os.readlink(file)
    return ''


# Add default metadata for paths missing from the source fsconfig.
def fs_patch(fs_file, dir_path) -> tuple:  # Compare the two metadata dictionaries.
    """
    Patch fs_file, Add Missing File Config
    :param fs_file:
    :param dir_path:
    :return:
    """
    new_fs = {}
    new_add = 0
    r_fs = deque()
    print(f"FsPatcher: The original file has {len(fs_file.keys()):d} entries")
    for i in scan_dir(os.path.abspath(dir_path)):
        if not i.isprintable():
            tmp = ''
            for c in i:
                tmp += c if c.isprintable() else '*'
            i = tmp.replace(' ', '*')
        if fs_file.get(i):
            new_fs[i] = fs_file[i]
        else:
            if i in r_fs:
                continue
            filepath = os.path.abspath(dir_path + os.sep + ".." + os.sep + i)
            if os.path.isdir(filepath):
                if "system/bin" in i or "system/xbin" in i or "vendor/bin" in i:
                    gid = '2000'
                else:
                    gid = '0'
                # dir path always 755
                config = ['0', gid, '0755']
            elif not os.path.exists(filepath):
                config = ['0', '0', '0755']
            elif islink(filepath):
                if ("system/bin" in i) or ("system/xbin" in i) or ("vendor/bin" in i):
                    gid = '2000'
                else:
                    gid = '0'
                if ("/bin" in i) or ("/xbin" in i):
                    mode = '0755'
                elif ".sh" in i:
                    mode = "0750"
                else:
                    mode = "0644"
                config = ['0', gid, mode, islink(filepath)]
            elif ("/bin" in i) or ("/xbin" in i):
                mode = '0755'
                if ("system/bin" in i) or ("system/xbin" in i) or ("vendor/bin" in i):
                    gid = '2000'
                else:
                    gid = '0'
                    mode = '0755'
                if ".sh" in i:
                    mode = "0750"
                else:
                    for s in ["/bin/su", "/xbin/su", "disable_selinux.sh", "daemon", "ext/.su", "install-recovery",
                              'installed_su', 'bin/rw-system.sh', 'bin/getSPL']:
                        if s in i:
                            mode = "0755"
                config = ['0', gid, mode]
            else:
                config = ['0', '0', '0644']
            print(f'Add [{i}{config}]')
            r_fs.append(i)
            new_add += 1
            new_fs[i] = config
    return new_fs, new_add


# Public fsconfig patching entry point used by EXT4/EROFS repacking.
def patch_fsconfig(dir_path: str, fs_config: str):
    """
    List The Dir_Path and Add Missing file config
    :param dir_path:
    :param fs_config:
    :return:
    """
    new_fs, new_add = fs_patch(scanfs(os.path.abspath(fs_config)), dir_path)
    with open(fs_config, "w", encoding='utf-8', errors='surrogateescape', newline='\n') as f:
        f.writelines([f"{i} {' '.join(new_fs[i])}\n" for i in sorted(new_fs.keys())])
    print(f'FsPatcher: Added {new_add} entries')


# ---------------------------------------------------------------------------
# Symlink restoration for workspaces extracted without the Windows symlink
# privilege.  `e2fsdroid` and `mkfs.erofs` take the inode type from the source
# tree, and `e2fsdroid` reports `ignored token` for the link target recorded in
# the fsconfig, so the link has to exist on disk before the image is built.
# ---------------------------------------------------------------------------
def _link_targets(fs_config: str) -> dict:
    """Map image path -> link target for every symlink entry in the fsconfig.

    The extractor writes the original target as a fifth field.  A capability
    set occupies the same position, so those lines are filtered out.
    """
    targets = {}
    with open(fs_config, "r", encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 5:
                continue
            path, _uid, _gid, _mode, *extra = fields
            if extra[0].startswith("capabilities="):
                continue
            # Re-join, because a link target may itself contain spaces.
            targets[path] = " ".join(extra)
    return targets


def _is_reparse_point(path: str) -> bool:
    """True for an NTFS junction or another reparse point."""
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        try:
            return bool(isjunction(path))
        except OSError:
            return False
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _is_materialized_link(path: str, target: str) -> bool:
    """True when `path` is a leftover of symlink materialization.

    Only two shapes count: a hard link (the extractor links to the resolved
    target) and a small placeholder file whose whole content is the target.
    Anything else may be a deliberate edit by the user and is left alone.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if info.st_nlink > 1:
        return True
    if info.st_size > 4096:
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8", "replace") == target
    except OSError:
        return False


def restore_symlinks(dir_path: str, fs_config: str) -> dict:
    """Recreate the symlinks declared by `fs_config` inside `dir_path`.

    Returns a report with the declared/restored/present counts plus the paths
    that were skipped (ambiguous) or failed (the OS refused to create a link).
    A non-empty `failed` list means the built image would contain a regular
    file where the original image had a symlink.
    """
    targets = _link_targets(fs_config)
    base = os.path.dirname(os.path.abspath(dir_path))
    report = {"declared": len(targets), "restored": 0, "present": 0,
              "failed": [], "skipped": []}
    for image_path, target in targets.items():
        path = os.path.join(base, *image_path.split("/"))
        if os.path.islink(path):
            report["present"] += 1
            continue
        if os.path.isdir(path):
            if not _is_reparse_point(path):
                report["skipped"].append(image_path)
                continue
        elif os.path.exists(path):
            if not _is_materialized_link(path, target):
                report["skipped"].append(image_path)
                continue

        # Build the link beside the entry first: when Windows refuses to create
        # symbolic links the materialized file must stay untouched, so a failed
        # attempt never turns a wrong file into a missing one.
        staging = path + ".art-link-staging"
        try:
            if os.path.lexists(staging):
                os.unlink(staging)
            resolved = os.path.join(base, *target.lstrip("/\\").split("/"))
            os.symlink(target, staging, target_is_directory=os.path.isdir(resolved))
        except (OSError, NotImplementedError) as error:
            report["failed"].append(f"{image_path}: {error}")
            continue
        try:
            if os.path.isdir(path):
                os.rmdir(path)  # removes the junction, never its target
            else:
                os.unlink(path)
            os.replace(staging, path)
        except OSError as error:
            report["failed"].append(f"{image_path}: {error}")
            try:
                os.unlink(staging)
            except OSError:
                pass
            continue
        report["restored"] += 1
    return report
