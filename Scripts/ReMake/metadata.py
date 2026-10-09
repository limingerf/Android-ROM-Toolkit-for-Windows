"""Normalize Android image metadata files for the bundled Windows tools.

The Android file-contexts readers used by the Windows toolchain require ASCII
input. OEM images can still contain UTF-8 or legacy byte sequences in path
names, so normalize only the path portion while preserving the SELinux label.
"""

from __future__ import annotations

import os
from pathlib import Path


def _escape_bytes(value: bytes) -> bytes:
    """Encode non-ASCII bytes as the octal escapes accepted by libselinux."""
    output = bytearray()
    for byte in value:
        if 0x20 <= byte < 0x7F or byte in (0x09,):
            output.append(byte)
        else:
            output.extend(f"\\{byte:03o}".encode("ascii"))
    return bytes(output)


def normalize_metadata(path: str | os.PathLike[str]) -> int:
    """Normalize and stably deduplicate a metadata file in place.

    Returns the number of source lines changed or discarded. Reading and
    writing is byte based so malformed OEM names do not abort repacking.
    """
    target = Path(path)
    raw = target.read_bytes()
    changed = 0
    seen: set[bytes] = set()
    normalized: list[bytes] = []
    context_file = target.name.endswith("_contexts.txt") or target.name.endswith("_file_contexts")
    for index, line in enumerate(raw.splitlines(keepends=True), 1):
        ending = b"\n" if line.endswith(b"\n") else b""
        body = line[:-1] if ending else line
        if body.endswith(b"\r"):
            body = body[:-1]
        if index == 1 and body.startswith(b"\xef\xbb\xbf"):
            body = body[3:]
            changed += 1
        if not body.strip():
            continue
        # file_contexts consists of a path regex followed by an ASCII SELinux
        # label. Escape only the path token there. fs_config is kept byte
        # faithful so Python's surrogateescape can match OEM filenames.
        if context_file:
            fields = body.split(None, 1)
            result = _escape_bytes(fields[0])
            if len(fields) == 2:
                result += b" " + fields[1]
        else:
            result = body
        if result != body:
            changed += 1
        result += ending or b"\n"
        key = result.rstrip(b"\r\n")
        if key in seen:
            changed += 1
            continue
        seen.add(key)
        normalized.append(result)
    target.write_bytes(b"".join(normalized))
    return changed
