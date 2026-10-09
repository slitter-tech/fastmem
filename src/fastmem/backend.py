"""Backend selection: C extension or pure Python.

Loads ``fastmem._fastmem`` when it has been built, otherwise silently uses
the ctypes implementation. The public API does not depend on which one is
active.
"""

import ctypes
from typing import List, Optional, Sequence

try:
    from . import _fastmem as _c
except ImportError:
    # Not built (no compiler, no MSVC, PyPy). This is not an error:
    # the library is fully functional on pure Python.
    _c = None

HAVE_C = _c is not None

# Building (ctypes.c_size_t * n)(*addrs) is not free, but it is much
# cheaper than the batch itself. The array is needed because C takes a
# ready uintptr_t buffer rather than a list of Python objects.
_size_t = ctypes.c_size_t

# NOTE: argtypes/restype do not apply here - this is a real CPython
# module, not a ctypes wrapper. _fastmem.c validates types itself through
# PyArg_ParseTupleAndKeywords.


def backend_name() -> str:
    """Active backend: ``'c-extension'`` or ``'python-ctypes'``."""
    return "c-extension" if HAVE_C else "python-ctypes"


def extension_version() -> Optional[str]:
    """Version of the C extension, or None when running pure Python."""
    return getattr(_c, "__version__", None) if HAVE_C else None


def is_alive(handle: Optional[int]) -> bool:
    """Whether the process is still running (via ``GetExitCodeProcess``).

    Exposed separately so callers can check liveness once per batch: in C
    this is its own function, because checking per address costs ~0.57 us
    and eats more than half of what the C loop saves.
    """
    if HAVE_C:
        return _c.is_alive(handle)

    from ctypes import byref, wintypes

    from ._winapi import STILL_ACTIVE, GetExitCodeProcess

    if not handle:
        return False
    code = wintypes.DWORD(0)
    if not GetExitCodeProcess(handle, byref(code)):
        return False
    return code.value == STILL_ACTIVE


def batch_into(
    handle: int,
    addresses: Sequence[int],
    size: int,
    out,
) -> bytes:
    """Read ``size`` bytes from each address into buffer ``out``.

    :param handle: process HANDLE.
    :param addresses: sequence of integer addresses.
    :param size: read size per address.
    :param out: buffer of at least ``len(addresses) * size`` bytes.
    :return: mask of length ``len(addresses)``: 1 on success, 0 on failure.
    """
    count = len(addresses)
    if HAVE_C:
        arr = (_size_t * count)(*addresses)
        # count is not passed: C derives it from the address buffer length
        # (abuf.len / sizeof(uintptr_t)), which saves an argument and makes
        # mismatched values impossible.
        _ok, mask = _c.batch_release(handle, arr, size, out)
        return bytes(mask)
    return _batch_into_py(handle, addresses, size, out, count)


def _batch_into_py(
    handle: int,
    addresses: Sequence[int],
    size: int,
    out,
    count: int,
) -> bytes:
    """Pure Python fallback. Same contract as the C version."""
    from . import _winapi as w

    rpm = w.ReadProcessMemory
    mask = bytearray(count)
    out_addr = out if isinstance(out, int) else ctypes.addressof(out)
    offset = 0
    for i, addr in enumerate(addresses):
        if rpm(handle, addr, out_addr + offset, size, None):
            mask[i] = 1
        offset += size
    return bytes(mask)


def batch_bytes(
    handle: int,
    addresses: Sequence[int],
    size: int,
) -> List[Optional[bytes]]:
    """Read addresses and return a ready list of ``bytes``/``None``.

    A separate entry point because slicing the result in Python
    (``bytes(mv[offset:offset+size])`` in a loop) costs one iteration per
    address and eats most of the gain: 2000 us against 750 us on 1000
    addresses when C builds the objects.
    """
    count = len(addresses)
    if HAVE_C:
        arr = (_size_t * count)(*addresses)
        return _c.batch_bytes(handle, arr, size)

    from . import _winapi as w

    rpm = w.ReadProcessMemory
    scratch = ctypes.create_string_buffer(size)
    scratch_addr = ctypes.addressof(scratch)
    string_at = ctypes.string_at
    out: List[Optional[bytes]] = []
    append = out.append
    for addr in addresses:
        if rpm(handle, addr, scratch_addr, size, None):
            append(string_at(scratch_addr, size))
        else:
            append(None)
    return out


def batch_grouped_bytes(
    handle: int,
    addresses: Sequence[int],
    size: int,
    span: int,
) -> List[Optional[bytes]]:
    """Group nearby addresses in C and return a ready list of bytes/None.

    Python-side slicing would eat the gain: 649 us against 86 us on 1000
    addresses when C builds the objects.
    """
    if not HAVE_C:
        raise RuntimeError("grouped batch reads require the C extension")
    count = len(addresses)
    arr = (_size_t * count)(*addresses)
    return _c.batch_grouped_bytes(handle, arr, size, span)


def single(handle: int, address: int, size: int, out) -> bool:
    """Single read into a reusable buffer ``out``.

    The GIL is intentionally not released: ``one_release`` measured
    slower than ``one`` (5.43 against 4.73 us over 2000 calls) because the
    GIL switch costs more than the operation.
    """
    if HAVE_C:
        return _c.one(handle, address, size, out)

    from . import _winapi as w

    return bool(w.ReadProcessMemory(handle, address, out, size, None))
