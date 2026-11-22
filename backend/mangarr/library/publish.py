"""Publish a completed file without overwriting an existing destination."""

import ctypes
import errno
import os
import sys
from pathlib import Path

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_AT2 = getattr(_LIBC, "renameat2", None) if sys.platform == "linux" else None
_RENAME_EXCLUSIVE = getattr(_LIBC, "renamex_np", None) if sys.platform == "darwin" else None
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4


def publish_new_file(staged: Path, destination: Path) -> None:
    """Atomically install a sibling staging file or raise FileExistsError."""
    source = os.fsencode(staged)
    target = os.fsencode(destination)
    if _RENAME_AT2 is not None:
        result = _RENAME_AT2(_AT_FDCWD, source, _AT_FDCWD, target, _RENAME_NOREPLACE)
    elif _RENAME_EXCLUSIVE is not None:
        result = _RENAME_EXCLUSIVE(source, target, _RENAME_EXCL)
    else:
        result = None
    if result == 0:
        return
    if result is not None:
        error = ctypes.get_errno()
        if error not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise OSError(error, os.strerror(error), str(destination))
    # Some filesystems lack exclusive rename but support hard links.
    os.link(staged, destination)
