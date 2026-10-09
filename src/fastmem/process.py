"""Process handle and the consolidated fastmem API.

The public surface is deliberately small: one read entry point, one batch
entry point, one region entry point, plus memory enumeration and search.
Type interpretation is handled by a single ``as_`` parameter instead of a
dozen ``read_*`` helpers.
"""

import ctypes
import os
import struct
from ctypes import wintypes
from typing import Iterable, Iterator, List, Optional, Sequence, Union

from . import _winapi as w
from . import backend
from .exceptions import (
    ProcessClosedError,
    ProcessOpenError,
    ProcessTerminatedError,
    ReadMemoryError,
)

# Reused scratch buffer for small reads: anything at or below this size
# avoids allocation entirely.
SCRATCH_SIZE = 4096

# ctypes callables cached at module level: going through self._x is far
# cheaper than a getattr chain on every call.
_string_at = ctypes.string_at
_addressof = ctypes.addressof
_get_last_error = ctypes.get_last_error
_unpack_from = struct.unpack_from
_byref = ctypes.byref


def _new_buf(size: int):
    """Allocate a buffer of ``size`` bytes (only for reads above SCRATCH_SIZE)."""
    return (ctypes.c_char * size)()


# --------------------------------------------------------------------------
# Type interpretation for the as_ parameter
# --------------------------------------------------------------------------

# name -> (byte width, precompiled struct.Struct)
#
# Struct objects are built once at import time: struct.unpack_from would
# otherwise look the format up in its internal cache on every read.
_FIXED_FORMATS = {
    "i8": (1, struct.Struct("<b")),
    "u8": (1, struct.Struct("<B")),
    "i16": (2, struct.Struct("<h")),
    "u16": (2, struct.Struct("<H")),
    "i32": (4, struct.Struct("<i")),
    "u32": (4, struct.Struct("<I")),
    "i64": (8, struct.Struct("<q")),
    "u64": (8, struct.Struct("<Q")),
    "f32": (4, struct.Struct("<f")),
    "f64": (8, struct.Struct("<d")),
    "float": (4, struct.Struct("<f")),
    "double": (8, struct.Struct("<d")),
}

# Pointer-sized formats, one per target width.
_PTR_FORMATS = {
    4: struct.Struct("<I"),
    8: struct.Struct("<Q"),
}

# Aliases for the common cases. int/long are UNSIGNED: memory reads are
# about raw values (hashes, flags, pointers), and 0xDEADBEEF coming back as
# -559038737 surprises everyone. Use i32/i64 for signed interpretation.
_ALIASES = {
    "int": "u32",
    "uint": "u32",
    "long": "u64",
    "ulong": "u64",
    "byte": "u8",
    "short": "i16",
    "ushort": "u16",
}


def _resolve(as_: Optional[str], pointer_size: int):
    """Map an ``as_`` name to (byte width, precompiled struct.Struct).

    :return: ``None`` for raw bytes, or the width and Struct for a number.
    :raises ValueError: unknown name.
    """
    if as_ is None:
        return None

    name = _ALIASES.get(as_, as_)
    if name == "ptr":
        # Pointer of the target process: 8 bytes on x64/ARM64, 4 on x86.
        width = 8 if pointer_size == 8 else 4
        return (width, _PTR_FORMATS[width])

    entry = _FIXED_FORMATS.get(name)
    if entry is None:
        raise ValueError(
            "unknown as_=%r; use None, 'ptr', 'int', 'uint', 'long', "
            "'ulong', 'float', 'double' or i8..u64" % (as_,)
        )
    return entry


# --------------------------------------------------------------------------
# Region
# --------------------------------------------------------------------------

class Region:
    """Immutable description of a chunk of process virtual memory.

    Built from ``MEMORY_BASIC_INFORMATION``.

    :ivar base: base address of the region.
    :ivar size: size in bytes.
    :ivar state: ``MEM_COMMIT`` / ``MEM_RESERVE`` / ``MEM_FREE``.
    :ivar protect: page protection flags (``PAGE_*``).
    :ivar type: ``MEM_PRIVATE`` / ``MEM_MAPPED`` / ``MEM_IMAGE``.
    """

    __slots__ = ("base", "size", "state", "protect", "type")

    def __init__(self, base: int, size: int, state: int = 0,
                 protect: int = 0, type_: int = 0) -> None:
        self.base = base
        self.size = size
        self.state = state
        self.protect = protect
        self.type = type_

    @classmethod
    def _from_mbi(cls, mbi) -> "Region":
        return cls(mbi.BaseAddress or 0, mbi.RegionSize, mbi.State,
                   mbi.Protect, mbi.Type)

    @property
    def end(self) -> int:
        """First address past the region."""
        return self.base + self.size

    @property
    def committed(self) -> bool:
        """True when the region is actually allocated (``MEM_COMMIT``)."""
        return self.state == w.MEM_COMMIT

    @property
    def readable(self) -> bool:
        """True when the region is readable."""
        return (self.protect & 0xFF) in w.PROT_READABLE

    @property
    def is_image(self) -> bool:
        """True for a loaded module image (``MEM_IMAGE``)."""
        return self.type == w.MEM_IMAGE

    @property
    def is_private(self) -> bool:
        """True for heap or stack (``MEM_PRIVATE``)."""
        return self.type == w.MEM_PRIVATE

    def contains(self, address: int) -> bool:
        """Whether ``address`` falls inside this region."""
        return self.base <= address < self.base + self.size

    def __repr__(self) -> str:
        flags = []
        if self.committed:
            flags.append("COMMIT")
        if self.is_image:
            flags.append("IMAGE")
        if self.is_private:
            flags.append("PRIVATE")
        return "<fastmem.Region 0x{:X}-0x{:X} ({} bytes) {}>".format(
            self.base, self.base + self.size, self.size, " ".join(flags)
        )


# --------------------------------------------------------------------------
# Pattern search
# --------------------------------------------------------------------------

def _find_all(blob: bytes, pattern: bytes, alignment: int) -> Iterator[int]:
    """Offsets of ``pattern`` in ``blob`` through the C ``bytes.find``.

    Alignment is checked arithmetically rather than by searching again:
    one ``&`` per hit instead of a second pass over the string.
    """
    find = blob.find
    pos = find(pattern)
    if alignment <= 1:
        while pos != -1:
            yield pos
            pos = find(pattern, pos + 1)
    else:
        mask = alignment - 1
        while pos != -1:
            if not pos & mask:
                yield pos
            pos = find(pattern, pos + 1)


# --------------------------------------------------------------------------
# Process
# --------------------------------------------------------------------------

class Process:
    """A process handle with fast memory reads.

    Not thread-safe: a reusable buffer lives inside. For manual multithread
    reading open one ``Process`` per thread, which is cheap (a single
    ``OpenProcess`` each). :meth:`read_many` is the exception - it manages
    its own threads and is safe to call.

    :param pid: target process id.
    :param access: desired access rights. Defaults to the minimum needed:
        ``PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION``
        (``PROCESS_VM_READ`` is what reading requires, and
        ``PROCESS_QUERY_LIMITED_INFORMATION`` is only used to detect the
        target bitness - unlike ``PROCESS_QUERY_INFORMATION`` it is granted
        to processes of the same user without elevation).
    :raises ProcessOpenError: the process does not exist or access was denied.
    """

    __slots__ = (
        "pid", "_handle", "_closed", "_machine", "_pointer_size", "_page_size",
        "_buf", "_buf_addr", "_buf_mv", "_rbuf", "_rbuf_addr", "_rbuf_mv",
        "_nread", "_nread_ptr", "_rpm", "_c_one", "_c_one_or",
    )

    def __init__(self, pid: int, access: Optional[int] = None) -> None:
        self.pid = int(pid)
        self._closed = False
        self._machine = w.IMAGE_FILE_MACHINE_UNKNOWN
        self._pointer_size = struct.calcsize("P")
        # Page size is read once: it cannot change while the system runs.
        self._page_size = w.page_size()

        handle = w.OpenProcess(
            w.ACCESS_READ_ONLY if access is None else access, False, self.pid
        )
        if not handle:
            raise ProcessOpenError(self.pid, _get_last_error())
        self._handle = handle

        # Reusable scratch buffer with its address and a memoryview cached:
        # the hot path then performs no allocation, no addressof() call and
        # no memoryview construction.
        self._buf = (ctypes.c_char * SCRATCH_SIZE)()
        self._buf_addr = _addressof(self._buf)
        self._buf_mv = memoryview(self._buf)
        # Separate buffer for region reads, also reused so that large
        # allocations do not pay a memset: (ctypes.c_char * n)() zeroes
        # memory, which for 1 MiB is ~400 us - comparable to the read itself.
        self._rbuf = (ctypes.c_char * SCRATCH_SIZE)()
        self._rbuf_addr = _addressof(self._rbuf)
        self._rbuf_mv = memoryview(self._rbuf)

        self._nread = ctypes.c_size_t(0)
        self._nread_ptr = ctypes.pointer(self._nread)
        self._rpm = w.ReadProcessMemory

        # C single-read entry points, or None when the extension is absent.
        # A separate variant returns bytes so the two hot shapes (a number
        # and raw bytes) avoid re-wrapping in Python.
        self._c_one = backend.single if backend.HAVE_C else None
        self._c_one_or = backend.single_bytes if backend.HAVE_C else None

        self._detect_machine()

    # ------------------------------------------------------------------
    # Target bitness
    # ------------------------------------------------------------------

    def _detect_machine(self) -> None:
        """Detect the architecture of the target process.

        Preference order:

        1. ``IsWow64Process2`` (Windows 10 1709 / Server 2019) returns both
           the process and the system machine, so x64, ARM64 and ARM64EC are
           told apart. ``IMAGE_FILE_MACHINE_UNKNOWN`` means the process is
           native.
        2. ``IsWow64Process`` (Vista+) plus the system architecture, for
           Win7/8/8.1 and Server 2008/2012.
        3. The system architecture as a last resort.

        Only public APIs are used, never hardcoded structure offsets.
        """
        handle = self._handle
        native = w.native_machine()

        if w.IsWow64Process2 is not None:
            process_machine = wintypes.WORD(0)
            system_machine = wintypes.WORD(0)
            if w.IsWow64Process2(
                handle,
                _byref(process_machine),
                _byref(system_machine),
            ):
                if system_machine.value:
                    native = system_machine.value
                machine = process_machine.value or native
                if machine:
                    self._machine = machine
                    self._pointer_size = (
                        8 if machine in w.MACHINES_64BIT else 4
                    )
                    return

        if w.IsWow64Process is not None:
            wow64 = wintypes.BOOL(0)
            if w.IsWow64Process(handle, _byref(wow64)) and wow64.value:
                # WOW64: a 32-bit process on a 64-bit system
                self._machine = w.IMAGE_FILE_MACHINE_I386
                self._pointer_size = 4
                return

        if native:
            self._machine = native
            self._pointer_size = 8 if native in w.MACHINES_64BIT else 4

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def machine(self) -> int:
        """``IMAGE_FILE_MACHINE_*`` of the target process."""
        return self._machine

    @property
    def machine_name(self) -> str:
        """Architecture name: 'x86', 'x64', 'ARM64', ..."""
        return w.MACHINE_NAMES.get(self._machine, "unknown")

    @property
    def is_64bit(self) -> bool:
        """True when the target process is 64-bit."""
        return self._pointer_size == 8

    @property
    def pointer_size(self) -> int:
        """Pointer size in the target process: 4 or 8 bytes."""
        return self._pointer_size

    @property
    def page_size(self) -> int:
        """System page size, from ``GetSystemInfo``."""
        return self._page_size

    @property
    def handle(self) -> Optional[int]:
        """Raw process HANDLE, or None once closed."""
        return self._handle

    @property
    def closed(self) -> bool:
        """True once the handle has been released."""
        return self._closed

    @staticmethod
    def backend() -> str:
        """Active backend: ``'c-extension'`` or ``'python-ctypes'``.

        Worth checking in tests and profiles: without the extension every
        batch method falls back to pure Python and runs 2.5-3x slower.
        """
        return backend.backend_name()

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the handle. Safe to call more than once."""
        handle = self._handle
        if handle:
            self._handle = None
            self._closed = True
            w.CloseHandle(handle)

    def __enter__(self) -> "Process":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return "<fastmem.Process pid={} {} {}>".format(
            self.pid, self.machine_name, "closed" if self._closed else "open"
        )

    def is_alive(self) -> bool:
        """Whether the target process is still running.

        :return: False when the process exited, the handle is invalid, or
            GetExitCodeProcess reported an exit code other than
            STILL_ACTIVE.
        """
        return backend.is_alive(self._handle)

    def _require_handle(self) -> int:
        handle = self._handle
        if not handle:
            raise ProcessClosedError(0, 0, 0)
        return handle

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(
        self,
        addr: int,
        size: Optional[int] = None,
        as_: Optional[str] = None,
        or_none: bool = False,
    ):
        """Read from ``addr``, optionally interpreted as a number.

        :param addr: address to read.
        :param size: bytes to read. ``None`` means the target pointer size,
            which makes ``read(ptr_addr)`` read a pointer directly.
        :param as_: ``None`` for raw ``bytes``; otherwise a type name -
            ``'ptr'``, ``'int'``, ``'uint'``, ``'long'``, ``'ulong'``,
            ``'float'``, ``'double'`` or explicit widths ``'i8'``..``'u64'``.
            ``'ptr'`` follows the target bitness, so it reads 8 bytes on
            x64/ARM64 and 4 on x86 - which matters for WOW64 targets.
        :param or_none: return None instead of raising when the address is
            unreadable or the process is gone.
        :return: ``bytes``, a number, or None.
        :raises ProcessClosedError: the handle was already released.
        :raises ReadMemoryError: the range is unreadable or partially unmapped.
        :raises ProcessTerminatedError: the process exited (subclass of
            ``ReadMemoryError``).

        Example::

            hp = p.read(addr, as_="float")
            obj = p.read(addr)          # 8 bytes on a 64-bit target
        """
        handle = self._handle
        if not handle:
            raise ProcessClosedError(addr, size or 0, 0)

        resolved = _resolve(as_, self._pointer_size)
        if resolved is not None:
            size, fmt = resolved
        elif size is None:
            size = self._pointer_size
            fmt = None
        else:
            fmt = None

        if size <= 0:
            if or_none:
                return None
            return b"" if fmt is None else None

        if size <= SCRATCH_SIZE:
            buf_addr = self._buf_addr
            buf_obj = self._buf
            # The whole view is passed, not a slice: C writes exactly
            # `size` bytes and reads nothing back from it, so a larger
            # buffer is harmless and saves a slice allocation per call.
            buf_mv = self._buf_mv
        else:
            # IMPORTANT: buf must stay alive until the call returns. Handing
            # out only addressof(_new_buf(size)) lets the temporary die
            # immediately and ReadProcessMemory writes into freed memory.
            buf = _new_buf(size)
            buf_addr = _addressof(buf)
            buf_obj = buf
            buf_mv = memoryview(buf)

        # C path. Passing the buffer as a writable memoryview lets the
        # extension accept it with y* and fill it directly, which avoids
        # building a ctypes c_char array just to satisfy an argument type.
        # Measured on a foreign process: 7.3 us via ctypes against ~2.5 us
        # through one().
        # C path. Two entry points because the hot shapes differ: a number
        # only needs a bool plus an unpack, while raw bytes are built in C
        # to skip a copy. Passing the buffer as a writable memoryview lets
        # the extension take it with y* and fill it directly, so no ctypes
        # c_char array has to be constructed just to satisfy an argument
        # type.
        # Measured on a foreign process: 7.3 us through ctypes against
        # ~2.5 us through the extension.
        if fmt is not None:
            if self._c_one is not None:
                if self._c_one(handle, addr, size, buf_mv):
                    return fmt.unpack_from(buf_obj)[0]
                if or_none:
                    return None
                raise self._error(addr, size)
        elif self._c_one_or is not None:
            got = self._c_one_or(handle, addr, size, buf_mv)
            if got is not None:
                return got
            if or_none:
                return None
            raise self._error(addr, size)

        # Pure Python path.
        #
        # lpNumberOfBytesRead = NULL: when it returns TRUE the read covered
        # exactly size bytes (a partial copy returns FALSE), so no c_size_t
        # is needed - that saves ~0.15 us and an allocation.
        if not self._rpm(handle, addr, buf_addr, size, None):
            if or_none:
                return None
            raise self._error(addr, size)

        if fmt is not None:
            return fmt.unpack_from(self._buf)[0]
        return _string_at(buf_addr, size)

    def read_many(
        self,
        addrs: Iterable[int],
        size: Optional[int] = None,
        as_: Optional[str] = None,
        into: bool = False,
        span: int = -1,
        threads: int = 0,
    ) -> Union[List[Optional[bytes]], List[Optional[int]], bytearray]:
        """Read many addresses at once.

        Uses the C extension when available, so Python crosses into C once
        instead of once per address. Without it this is plain ctypes and
        runs 2.5-3x slower.

        :param addrs: any iterable of addresses.
        :param size: bytes per address. ``None`` means the target pointer
            size, matching :meth:`read`.
        :param as_: type name as in :meth:`read`; produces a list of numbers
            instead of ``bytes``.
        :param into: return a single ``bytearray`` of ``len(addrs) * size``
            instead of a list. No per-address object is created, which is
            the fastest option when the data feeds ``array.array`` or
            ``struct.unpack_from``. Unreadable addresses come back as zeros.
        :param span: group addresses whose distance is below ``span`` bytes
            and read each group with a single call. ``-1`` (default) uses
            four pages, which covers a typical object; ``0`` disables
            grouping. Nearby addresses are the common case in real work -
            object fields, array elements, list nodes - and grouping is up
            to 23x faster than reading them one by one.
        :param threads: helper threads for the read. ``0`` means one; above
            one, the addresses are split across persistent worker threads
            and the calling thread takes a share too. ``-1`` uses
            ``os.cpu_count()``. Requires the C extension: without it the
            GIL serialises the reads and ``threads>1`` raises rather than
            quietly running slower.

            The workers are created once and reused, so this costs nothing
            after the first call. Measured on a foreign process with 4
            helper threads: 2.6x on dense addresses, 1.8x on ones too
            sparse to group, against 1.11x and 0.20x when the threads were
            built per call. Grouping still wins for dense input, where one
            read per window beats one read per address by far more than
            threads do.
        :return: list of ``bytes``/None, list of numbers/None, or bytearray.
        :raises ProcessClosedError: the handle was already released.

        Example::

            values = p.read_many(addrs, as_="ptr")   # list[int | None]
            raw    = p.read_many(addrs, into=True)   # one bytearray
        """
        handle = self._require_handle()

        resolved = _resolve(as_, self._pointer_size)
        if resolved is not None:
            size, fmt = resolved
        elif size is None:
            size = self._pointer_size
            fmt = None
        else:
            fmt = None

        addrs = addrs if isinstance(addrs, (list, tuple)) else list(addrs)
        count = len(addrs)
        if count == 0:
            return bytearray() if into else []
        if size <= 0:
            return bytearray() if into else [None] * count

        if span < 0:
            span = self._page_size << 2

        # threads wins over grouping when both are available: the caller
        # asked for parallelism explicitly. Note that for dense addresses
        # grouping is usually the faster of the two (one read per window
        # instead of one per address), so threads= pays off mainly on
        # sparse input where nothing can be merged.
        if threads and threads > 1:
            # Raised even though a single-threaded fallback exists: the GIL
            # serialises the reads, so honouring threads= would return
            # slower than threads=1 and hide the reason.
            if not backend.HAVE_C:
                raise RuntimeError(
                    "threads>1 requires the C extension: without it the GIL "
                    "makes threaded reads slower, not faster"
                )
            if into:
                return self._read_many_into(handle, addrs, size, threads)
            result = self._read_many_threaded(handle, addrs, size, threads)
        elif into:
            return self._read_many_into(handle, addrs, size)
        elif span and backend.HAVE_C:
            # Sorting, grouping and object creation all happen in C. Doing
            # the slicing in Python costs 649 us against 86 us per 1000
            # addresses, i.e. it would eat the entire gain.
            result = backend.batch_grouped_bytes(handle, addrs, size, span)
        elif backend.HAVE_C:
            result = backend.batch_bytes(handle, addrs, size)
        else:
            result = self._read_many_py(handle, addrs, size)

        if fmt is None:
            return result
        unpack = fmt.unpack_from
        return [unpack(b)[0] if b is not None else None for b in result]

    def _read_many_py(self, handle, addrs, size) -> List[Optional[bytes]]:
        """Pure Python batch, used when the extension is absent."""
        buf_addr = (self._buf_addr if size <= SCRATCH_SIZE
                    else _addressof(_new_buf(size)))
        rpm = self._rpm
        string_at = _string_at
        out: List[Optional[bytes]] = []
        append = out.append
        for addr in addrs:
            if rpm(handle, addr, buf_addr, size, None):
                append(string_at(buf_addr, size))
            else:
                append(None)
        return out

    def _read_many_into(self, handle, addrs, size, threads=0) -> bytearray:
        """Batch into one bytearray; no Python-side object per address."""
        count = len(addrs)
        dst = bytearray(count * size)
        view = (ctypes.c_char * len(dst)).from_buffer(dst)
        if backend.HAVE_C:
            if threads and threads > 1:
                backend.pool_into(handle, addrs, size, view, threads)
            else:
                backend.batch_into(handle, addrs, size, view)
        else:
            rpm = self._rpm
            offset = 0
            for addr in addrs:
                rpm(handle, addr, _byref(view, offset), size, None)
                offset += size
        return dst

    def _read_many_threaded(self, handle, addrs, size, threads) -> List:
        """Spread addresses over the persistent worker pool.

        Requires the C extension. The workers live in C and are created once
        on first use; building threading.Thread objects per call instead cost
        ~812 us on Windows, more than the whole read, which is why threads
        used to be a net loss (measured 1.11-1.16x, and 0.20x on sparse
        input where it regressed below single-threaded).
        """
        if not backend.HAVE_C:
            raise RuntimeError(
                "threads>1 requires the C extension: without it the GIL "
                "makes threaded reads slower, not faster"
            )
        count = len(addrs)
        if threads <= 0:
            threads = (os.cpu_count() or 1)
        # One worker is the floor: 0 would mean "auto", but a single helper
        # thread plus the caller is already two-way parallelism.
        if threads < 1:
            threads = 1
        if threads > count - 1:
            # The calling thread always takes a chunk, so more helpers than
            # count-1 would leave some of them with no work at all.
            threads = count - 1
        if threads <= 0:
            return backend.batch_bytes(handle, addrs, size)
        return backend.pool_bytes(handle, addrs, size, threads)

    # ------------------------------------------------------------------
    # Regions
    # ------------------------------------------------------------------

    def read_region(
        self,
        base: int,
        size: int,
        chunks: int = 0,
        or_none: bool = False,
    ):
        """Read a contiguous range with a single call.

        The main primitive for scanners: walking every mapped region costs
        one call per region instead of thousands per page.

        :param base: start address.
        :param size: bytes to read.
        :param chunks: 0 reads the whole range at once; a positive value
            yields a generator of blocks of that size instead, so a large
            region never has to be held in memory at once. Blocks are cut
            at address-aligned boundaries to avoid splitting a page.
        :param or_none: return None instead of raising on failure.
        :return: ``bytes``, None, or a generator of ``bytes``.
        :raises ProcessClosedError: the handle was already released.
        :raises ReadMemoryError: the range is fully or partially unmapped.
        """
        handle = self._handle
        if not handle:
            raise ProcessClosedError(base, size, 0)

        if chunks and chunks > 0:
            return self._read_region_chunks(handle, base, size, chunks)
        if size <= 0:
            return None if or_none else b""

        buf_addr = self._region_buf(size)
        # lpNumberOfBytesRead is needed here: racing with the target, RPM
        # can return TRUE with a smaller count, and silently returning
        # truncated data would be wrong.
        nread = self._nread
        if not self._rpm(handle, base, buf_addr, size, self._nread_ptr):
            if or_none:
                return None
            raise self._error(base, size)
        # bytes(memoryview) is cheaper than string_at: one copy, not two.
        return bytes(self._rbuf_mv[: nread.value or size])

    def _region_buf(self, size: int) -> int:
        """Address of the reusable region buffer, grown as needed.

        The buffer never shrinks: once a scanner has read a 4 MiB region,
        later small reads pay no allocation.
        """
        buf = self._rbuf
        if ctypes.sizeof(buf) < size:
            # Keep the reference in self._rbuf, otherwise the buffer dies
            # before RPM is done with it.
            buf = _new_buf(size)
            self._rbuf = buf
            self._rbuf_addr = _addressof(buf)
            self._rbuf_mv = memoryview(buf)
        return self._rbuf_addr

    def _read_region_chunks(self, handle, base, size, chunk_size):
        """Generator over fixed-size blocks of a region."""
        if size <= 0:
            return
        buf_addr = self._region_buf(chunk_size)
        mv = self._rbuf_mv
        offset = 0
        while offset < size:
            n = chunk_size if size - offset > chunk_size else size - offset
            if self._rpm(handle, base + offset, buf_addr, n, None):
                yield bytes(mv[:n])
            offset += n

    # ------------------------------------------------------------------
    # Virtual memory enumeration
    # ------------------------------------------------------------------

    def query(self, addr: int) -> Optional[Region]:
        """Describe the region containing ``addr``.

        A ``VirtualQueryEx`` wrapper.

        :return: a :class:`Region`, or None when the address is outside the
            process address space or the API is unavailable.
        """
        handle = self._handle
        if not handle:
            raise ProcessClosedError(addr, 0, 0)
        if w.VirtualQueryEx is None:
            return None
        mbi = w.shared_mbi
        if not w.VirtualQueryEx(handle, addr, _byref(mbi), w.mbi_size):
            return None
        return Region._from_mbi(mbi)

    def regions(
        self,
        min_size: int = 0,
        max_size: int = 0,
        readable_only: bool = True,
        committed_only: bool = True,
        start: int = 0,
        end: Optional[int] = None,
    ) -> Iterator[Region]:
        """Iterate the process virtual memory regions.

        A single ``VirtualQueryEx`` costs ~2 us, so a full walk of a typical
        64-bit process (300-500 regions) takes under 2 ms. Each region is
        then read with one :meth:`read_region` call.

        :param min_size: skip regions smaller than this.
        :param max_size: truncate larger regions to this size (0 = no
            truncation). Useful for avoiding huge ``MEM_COMMIT`` regions.
        :param readable_only: skip regions without ``PROTECT_READ``.
        :param committed_only: skip ``MEM_FREE`` / ``MEM_RESERVE``.
        :param start: first address to inspect.
        :param end: stop before this address (None = whole address space).
        :return: generator of :class:`Region`.
        :raises ProcessClosedError: the handle was already released.
        """
        handle = self._handle
        if not handle:
            raise ProcessClosedError(start, 0, 0)
        if w.VirtualQueryEx is None:
            return

        vq = w.VirtualQueryEx
        mbi = w.shared_mbi
        addr = start

        while True:
            if not vq(handle, addr, _byref(mbi), w.mbi_size):
                return
            base = mbi.BaseAddress or 0
            span = mbi.RegionSize
            if span == 0:
                return

            keep = True
            if committed_only and mbi.State != w.MEM_COMMIT:
                keep = False
            if keep and readable_only and (mbi.Protect & 0xFF) not in (
                w.PROT_READABLE
            ):
                keep = False
            if keep and span < min_size:
                keep = False

            if keep and (end is None or base < end):
                if max_size and span > max_size:
                    yield Region(base, max_size, mbi.State, mbi.Protect,
                                 mbi.Type)
                else:
                    yield Region._from_mbi(mbi)

            nxt = base + span
            if nxt <= addr:
                return
            addr = nxt
            if end is not None and addr >= end:
                return

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find(
        self,
        pattern: bytes,
        regions: Optional[Iterable[Region]] = None,
        align: int = 1,
        limit: int = 0,
        chunk_size: int = 0,
    ) -> Iterator[int]:
        """Find every occurrence of ``pattern`` in process memory.

        The search runs in C through ``bytes.find``: locating a pattern
        inside a megabyte region takes ~0.3 us, while a Python loop over the
        bytes is five orders of magnitude slower. This is the single
        biggest factor in scanner speed - read a region, then search it.

        :param pattern: bytes to look for.
        :param regions: regions to search. ``None`` searches every readable
            committed region.
        :param align: 1 matches at any offset; 4 or 8 only at aligned
            addresses (faster and fewer false positives when hunting values).
        :param limit: stop after this many hits (0 = no limit).
        :param chunk_size: read in blocks of this size with overlap, so
            hits straddling a block boundary are not lost and the whole
            region never has to be resident. 0 reads each region whole.
        :return: generator of match addresses.
        """
        if not pattern:
            return

        if regions is None:
            regions = self.regions()

        found = 0
        needle = len(pattern)

        for reg in regions:
            base = reg.base
            remaining = reg.size

            if chunk_size <= 0:
                blob = self.read_region(base, remaining, or_none=True)
                if blob:
                    for off in _find_all(blob, pattern, align):
                        yield base + off
                        found += 1
                        if limit and found >= limit:
                            return
                continue

            # Blocked read: overlap by needle-1 bytes so a match spanning
            # two blocks survives.
            offset = 0
            tail = b""
            tail_addr = 0
            while offset < remaining:
                n = chunk_size if remaining - offset > chunk_size \
                    else remaining - offset
                blob = self.read_region(base + offset, n, or_none=True)
                if blob:
                    window = tail + blob
                    window_addr = tail_addr if tail else base + offset
                    limit_off = len(window) - needle + 1
                    for off in _find_all(window, pattern, align):
                        if limit_off > 0 and off >= limit_off:
                            break
                        yield window_addr + off
                        found += 1
                        if limit and found >= limit:
                            return
                    keep = needle - 1
                    if keep > 0 and len(window) > keep:
                        tail = window[-keep:]
                        tail_addr = window_addr + len(window) - keep
                    else:
                        tail = window
                        tail_addr = window_addr
                offset += n

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def _error(self, address: int, size: int) -> ReadMemoryError:
        """Build the exception. The message is formatted lazily in __str__."""
        # backend.last_error() reads the extension's own slot when C is
        # active; ctypes.get_last_error() cannot see into the extension and
        # would report 0 for every failure.
        code = backend.last_error()
        if code in (w.ERROR_INVALID_HANDLE,):
            return ProcessTerminatedError(address, size, code)
        return ReadMemoryError(address, size, code)
