"""Super image repacker for dynamic partitions."""

import os

from Scripts.Primary.Utils import V, call
from Scripts.Primary.ImageTools import (
    is_sparse_image, sparse_to_raw, raw_to_sparse,
)
from Scripts.Primary.Console import CoastTime, display
from Scripts.Primary.WorkSpace import LayoutError


# Locate the other slot's image and expand sparse inputs before lpmake.
def _slot_image(input_dir, name, slot):
    """Return the best image for ``<name>_<slot>``, including sparse variants."""
    for suffix in ('.img', '.sparse.img', '.raw.img'):
        candidate = os.path.join(input_dir, f'{name}_{slot}{suffix}')
        if os.path.isfile(candidate):
            return candidate
    return None


def _raw_image(path, workspace, temporary):
    """Return a raw image path, converting sparse input if needed.

    ``None`` means the conversion failed; the caller aborts instead of handing
    a sparse container to lpmake, which would embed its header as partition
    data and still report success.
    """
    if not is_sparse_image(path):
        return path
    display(f'转换 sparse: {os.path.basename(path)} ...')
    raw_path = os.path.join(workspace, f'.{os.path.basename(path)}.raw.img')
    raw = sparse_to_raw(path, raw_path, temp_dir=workspace)
    if not raw or not os.path.isfile(raw):
        print(f'> 无法转换 sparse 镜像: {path}')
        return None
    temporary.append(raw)
    return raw


# Build lpmake arguments and emit super.img.
def repack_super(selected_parts, super_type, super_sparse):
    """Synthesize super.img from selected partition images.

    Args:
        selected_parts: list of (name, path) tuples from INPUT
        super_type: 0=A-only, 1=A/B, 2=Virtual A/B
        super_sparse: 1=sparse output, 0=raw output

    Returns True only when ``super.img`` was produced completely.  A failed
    run never leaves a partial image behind, so the worker cannot publish a
    truncated container as a successful result.
    """

    group_name = V.SETUP_MANIFEST['GROUP_NAME']
    super_size = V.SETUP_MANIFEST['SUPER_SIZE']
    super_output = os.path.join(V.out, 'super.img')
    # Build into a hidden staging file first: lpmake can leave a truncated
    # container behind when it fails, and that partial file used to be
    # published as a successful OUT/super.img.raw.  Replacing the final path
    # only after a complete build also keeps a previous good super.img alive.
    build_output = os.path.join(V.out, '.super.img.build')
    type_names = {0: 'A-only', 1: 'A/B', 2: 'Virtual A/B'}
    temporary = []

    def cleanup():
        for path in (*temporary, build_output):
            try:
                os.remove(path)
            except OSError:
                pass

    argvs = [
        'lpmake',
        '--metadata-size', '65536',
        '--super-name', 'super',
        '--device', f'super:{super_size}',
    ]
    image_parts = []

    raw_parts = []
    try:
        seen = set()
        for name, path in selected_parts:
            if name in seen:
                # Both slots selected: the A/B builder derives the other slot
                # from INPUT, so a duplicate would define <name>_a twice and
                # make lpmake reject the whole metadata.
                continue
            seen.add(name)
            raw = _raw_image(path, V.workspace, temporary)
            if raw is None:
                cleanup()
                return False
            raw_parts.append((name, raw))
    except (LayoutError, OSError) as error:
        print(f'> 准备 super 镜像失败: {error}')
        cleanup()
        return False

    input_dir = V.input

    try:
        if super_type == 0:
            argvs.extend(['--metadata-slots', '2',
                          '--group', f'{group_name}:{super_size}'])
            for name, path in raw_parts:
                size = os.path.getsize(path)
                argvs.extend([
                    '--partition', f'{name}:readonly:{size}:{group_name}',
                    '--image', f'{name}={path}',
                ])
                image_parts.append(name)
        elif super_type == 1:
            argvs.extend(['--metadata-slots', '3',
                          '--group', f'{group_name}_a:{super_size}',
                          '--group', f'{group_name}_b:{super_size}'])
            for name, path in raw_parts:
                size_a = os.path.getsize(path)
                argvs.extend([
                    '--partition', f'{name}_a:readonly:{size_a}:{group_name}_a',
                    '--image', f'{name}_a={path}',
                ])
                # Accept the sparse/raw spellings the GUI and CLI produce;
                # only checking <name>_b.img silently emitted a zero-size B
                # slot for every .sparse.img bundle.
                b_path = _slot_image(input_dir, name, 'b')
                if b_path:
                    b_raw = _raw_image(b_path, V.workspace, temporary)
                    if b_raw is None:
                        cleanup()
                        return False
                    size_b = os.path.getsize(b_raw)
                    argvs.extend([
                        '--partition', f'{name}_b:readonly:{size_b}:{group_name}_b',
                        '--image', f'{name}_b={b_raw}',
                    ])
                else:
                    argvs.extend([
                        '--partition', f'{name}_b:readonly:0:{group_name}_b',
                    ])
                image_parts.append(name)
        else:
            argvs.extend(['--metadata-slots', '3', '--virtual-ab', '-F',
                          '--group', f'{group_name}_a:{super_size}',
                          '--group', f'{group_name}_b:{super_size}'])
            for name, path in raw_parts:
                size = os.path.getsize(path)
                argvs.extend([
                    '--partition', f'{name}_a:readonly:{size}:{group_name}_a',
                    '--image', f'{name}_a={path}',
                    '--partition', f'{name}_b:readonly:0:{group_name}_b',
                ])
                image_parts.append(name)
    except (LayoutError, OSError) as error:
        print(f'> 准备 super 镜像失败: {error}')
        cleanup()
        return False

    if not image_parts:
        print('> 未选择任何分区镜像')
        cleanup()
        return False

    # lpmake always creates the raw container here; utils.py owns the
    # final raw -> sparse conversion when the user requests sparse output.
    argvs.extend(['--out', build_output])

    display(f'重新合成: super.img <Size:{super_size}|Type:{type_names[super_type]}|Sparse:{super_sparse}>')
    display(f"包含分区：{'|'.join(image_parts)}")
    with CoastTime():
        result = call(argvs)
    if result != 0 or not os.path.isfile(build_output):
        print('> super.img 合成失败')
        cleanup()
        return False

    try:
        if super_sparse == 1:
            raw_to_sparse(build_output, super_output, temp_dir=V.out)
        else:
            os.replace(build_output, super_output)
    except (OSError, ValueError) as error:
        print(f'> 生成 super.img 失败: {error}')
        cleanup()
        return False

    cleanup()
    print(f'> super.img 已输出到 {V.out}')
    return True
