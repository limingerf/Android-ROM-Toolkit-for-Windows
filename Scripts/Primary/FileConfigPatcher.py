"""Android fsconfig scanning and metadata patching."""

import os
import re
import stat
from collections import Counter, deque

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
# SELinux label completion for workspaces that gained new paths.
# ---------------------------------------------------------------------------
# Only these suffixes follow a config file in the same directory; every other
# path follows the regular files beside it.  Extend the tuple to cover more
# config-like formats.
CONFIG_SUFFIXES = (".prop", ".rc", ".xml", ".conf", ".cfg", ".ini", ".json")

# The extractor escapes exactly these characters when it writes a path as a
# file_contexts pattern; the patcher must produce identical text.
_CONTEXT_SPECIALS = '\\^$.|?*+(){}[]'


def escape_context_path(path: str) -> str:
    """Escape a path so its file_contexts pattern matches only that path."""
    for character in _CONTEXT_SPECIALS:
        path = path.replace(character, '\\' + character)
    return path


def _has_active_metacharacter(pattern: str) -> bool:
    """True when `pattern` still holds an unescaped regex metacharacter."""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 2
            continue
        if character in ".*+?()[]{}|^$":
            return True
        index += 1
    return False


def _unescape_pattern(pattern: str) -> str:
    """Drop backslashes so both file_contexts escaping styles compare equal."""
    output = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            output.append(pattern[index + 1])
            index += 2
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _fullmatch(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        return False


class _ContextRules:
    """Ordered file_contexts rules with literal and regex lookup.

    libselinux keeps the last matching rule, so every lookup compares rule
    positions instead of stopping at the first hit.
    """

    def __init__(self, path: str):
        self.literals = {}
        self.unescaped = {}
        self.generic = []
        self.regexes = []
        index = 0
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                body = line.strip()
                if not body:
                    continue
                fields = body.split(None, 1)
                if len(fields) != 2 or not fields[1].strip():
                    continue
                pattern, label = fields[0], fields[1].strip()
                # libselinux keeps the last match, so a repeated pattern keeps
                # the position of its final occurrence.
                self.literals[pattern] = (index, label)
                self.unescaped[_unescape_pattern(pattern)] = (index, label)
                if "(/.*)" in pattern:
                    self.generic.append((index, pattern, label))
                elif _has_active_metacharacter(pattern):
                    self.regexes.append((index, pattern, label))
                index += 1

    @staticmethod
    def _best(matches):
        found = None
        for entry in matches:
            if found is None or entry[0] > found[0]:
                found = entry
        return found

    def _literal(self, image_path: str):
        matches = []
        for key in (escape_context_path(image_path), image_path):
            for table in (self.literals, self.unescaped):
                found = table.get(key)
                if found is not None:
                    matches.append(found)
        return self._best(matches)

    def specific(self, image_path: str):
        """Label from a rule that names this path, ignoring catch-alls."""
        matches = []
        literal = self._literal(image_path)
        if literal is not None:
            matches.append(literal)
        for index, pattern, label in self.regexes:
            if _fullmatch(pattern, image_path):
                matches.append((index, label))
        found = self._best(matches)
        return found[1] if found else None

    def resolve(self, image_path: str):
        """Label libselinux picks for the path, catch-alls included."""
        matches = []
        literal = self._literal(image_path)
        if literal is not None:
            matches.append(literal)
        for index, pattern, label in (*self.generic, *self.regexes):
            if _fullmatch(pattern, image_path):
                matches.append((index, label))
        found = self._best(matches)
        return found[1] if found else None


def _workspace_entries(dir_path: str) -> tuple:
    """Map every extracted path to its kind plus each directory's children."""
    root = os.path.abspath(dir_path)
    prefix = os.path.basename(root)
    entries = {f"/{prefix}": "dir"}
    children = {}
    for current, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        # A symlinked or junctioned directory is an entry of its parent, never a
        # subtree: descending into it would walk outside the workspace.
        links = [item for item in dirs
                 if os.path.islink(os.path.join(current, item))
                 or _is_reparse_point(os.path.join(current, item))]
        for name in links:
            dirs.remove(name)
            files.append(name)
        files.sort()
        relative = os.path.relpath(current, root)
        head = prefix if relative == os.curdir else f"{prefix}/{relative.replace(os.sep, '/')}"
        image_dir = f"/{head}"
        bucket = children.setdefault(image_dir, [])
        for name in dirs:
            bucket.append((name, "dir"))
            entries[f"{image_dir}/{name}"] = "dir"
        for name in files:
            bucket.append((name, "file"))
            entries[f"{image_dir}/{name}"] = "file"
    return entries, children


def _child_label(rules, children, image_dir: str, kind: str, suffixes=None):
    """Most common specific label among the children of `image_dir`.

    Children that only match the partition-wide catch-all do not vote, so a
    new path never inherits the fallback label from an unlabelled sibling.
    """
    counter = Counter()
    base = image_dir.rstrip("/")
    for name, child_kind in sorted(children.get(image_dir, ())):
        if child_kind != kind:
            continue
        if suffixes is not None and not name.lower().endswith(suffixes):
            continue
        label = rules.specific(f"{base}/{name}")
        if label:
            counter[label] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _guess_label(rules, children, image_path: str, kind: str):
    """Pick a default label and report which rule produced it."""
    image_dir, _, name = image_path.rpartition("/")
    image_dir = image_dir or "/"
    if kind == "dir":
        label = _child_label(rules, children, image_dir, "dir")
        if label:
            return label, "同目录目录"
    else:
        if name.lower().endswith(CONFIG_SUFFIXES):
            label = _child_label(rules, children, image_dir, "file", CONFIG_SUFFIXES)
            if label:
                return label, "同目录配置文件"
        label = _child_label(rules, children, image_dir, "file")
        if label:
            return label, "同目录常规文件"
    # The containing directory's own label, then every ancestor up to the root.
    probe = image_dir
    while True:
        label = rules.resolve(probe)
        if label:
            return label, "父目录" if probe == image_dir else "上级目录"
        if probe in ("", "/"):
            break
        probe = probe.rpartition("/")[0] or "/"
    return None, None


def patch_contexts(dir_path: str, contexts_path: str) -> int:
    """Give every extracted path an explicit SELinux rule.

    Paths that already carry a rule are left untouched.  A new path inherits a
    label so the generated line can be corrected in place instead of being
    written from scratch: config-like files follow the config files in their
    directory, other files follow the regular files beside them, and a
    directory follows a sibling directory and then its parent.

    Appended rules are written last, so they win over the partition-wide
    catch-all while libselinux keeps the last match.
    """
    rules = _ContextRules(contexts_path)
    entries, children = _workspace_entries(dir_path)
    added = []
    skipped = 0
    for image_path in sorted(entries):
        if rules.specific(image_path):
            continue
        label, source = _guess_label(rules, children, image_path, entries[image_path])
        if not label:
            skipped += 1
            print(f'[W] ContextPatcher: {image_path} 找不到可继承的标签，已跳过')
            continue
        added.append((escape_context_path(image_path), label, image_path, source))
    if not added:
        if skipped:
            print(f'ContextPatcher: 元数据里没有可继承的标签，跳过 {skipped} 个路径')
        else:
            print('ContextPatcher: 所有路径都已有标签，无需补充')
        return 0
    for _pattern, _label, image_path, source in added[:40]:
        print(f'Add [{image_path} -> {_label}] ({source})')
    if len(added) > 40:
        print(f'ContextPatcher: 其余 {len(added) - 40} 条已写入 {contexts_path}')
    with open(contexts_path, "rb") as probe:
        existing = probe.read()
    with open(contexts_path, "a", encoding="utf-8", errors="surrogateescape", newline="\n") as handle:
        if existing and not existing.endswith(b"\n"):
            handle.write("\n")
        for pattern, label, _image_path, _source in added:
            handle.write(f"{pattern} {label}\n")
    counts = Counter(source for *_rest, source in added)
    summary = "，".join(f"{name} {count}" for name, count in counts.items())
    print(f'ContextPatcher: 补充 {len(added)} 条标签（{summary}）')
    return len(added)


def persist_context_rules(project_path: str, completed_path: str) -> int:
    """Copy completed rules into the project metadata.

    Repacking works on a staged copy of the metadata, so the labels added by
    `patch_contexts` would disappear with the job directory.  Appending the
    missing lines keeps them visible and editable in the project file without
    rewriting anything the user already wrote.
    """
    existing = set()
    try:
        with open(project_path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            for line in handle:
                body = line.lstrip("\ufeff").strip()
                if body:
                    existing.add(body)
                    existing.add(_unescape_pattern(body))
    except OSError as error:
        print(f'[W] ContextPatcher: 无法读取 {project_path}: {error}')
        return 0
    added = []
    with open(completed_path, "r", encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            body = line.lstrip("\ufeff").strip()
            if not body or body in existing or _unescape_pattern(body) in existing:
                continue
            existing.add(body)
            existing.add(_unescape_pattern(body))
            added.append(body)
    if not added:
        return 0
    with open(project_path, "rb") as probe:
        data = probe.read()
    with open(project_path, "a", encoding="utf-8", errors="surrogateescape", newline="\n") as handle:
        if data and not data.endswith(b"\n"):
            handle.write("\n")
        for line in added:
            handle.write(line + "\n")
    print(f'ContextPatcher: 已把 {len(added)} 条补齐标签写回 {os.path.basename(project_path)}')
    return len(added)


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
