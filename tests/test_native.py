"""Regression tests for the standalone C library.

Loads ``build/native/fastmem.dll`` with ctypes. The findings here are
C-only: the grouped-read mask and error contract, pooled success counts,
the first-use pool initialisation race, and the explicit access mask.

Build first (``python tools/build_native.py``), then run:
    python -m tests.test_native
"""

import ctypes
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLL = os.path.join(ROOT, "build", "native", "fastmem.dll")

SEP = "=" * 74
FM_ALL_ACCESS = 0x001FFFFF


def ok(label):
    print("[{}] OK".format(label))


def load():
    lib = ctypes.CDLL(DLL)

    size_t = ctypes.c_size_t
    uptr = ctypes.POINTER(size_t)
    bytep = ctypes.POINTER(ctypes.c_ubyte)

    lib.fastmem_open.argtypes = [ctypes.c_uint]
    lib.fastmem_open.restype = ctypes.c_void_p
    lib.fastmem_open_ex.argtypes = [ctypes.c_uint, ctypes.c_uint]
    lib.fastmem_open_ex.restype = ctypes.c_void_p
    lib.fastmem_close.argtypes = [ctypes.c_void_p]
    lib.fastmem_close.restype = None
    lib.fastmem_is_alive.argtypes = [ctypes.c_void_p]
    lib.fastmem_is_alive.restype = ctypes.c_int
    lib.fastmem_read_grouped.argtypes = [
        ctypes.c_void_p, uptr, size_t, size_t, size_t,
        ctypes.c_void_p, bytep]
    lib.fastmem_read_grouped.restype = size_t
    lib.fastmem_pool_read_many.argtypes = [
        ctypes.c_void_p, uptr, size_t, size_t, ctypes.c_void_p, bytep,
        ctypes.c_uint]
    lib.fastmem_pool_read_many.restype = size_t
    lib.fastmem_pool_workers.argtypes = []
    lib.fastmem_pool_workers.restype = ctypes.c_uint
    lib.fastmem_pool_shutdown.argtypes = []
    lib.fastmem_pool_shutdown.restype = None
    lib.fastmem_last_error.argtypes = []
    lib.fastmem_last_error.restype = ctypes.c_ulong
    return lib


def addr_array(addresses):
    return (ctypes.c_size_t * len(addresses))(*addresses)


def mask_array(count, fill=0):
    return (ctypes.c_ubyte * count)(*([fill] * count))


def check_access_mask(lib, pid):
    proc = lib.fastmem_open_ex(pid, FM_ALL_ACCESS)
    assert proc, "fastmem_open_ex(FM_ALL_ACCESS) failed with {}".format(
        lib.fastmem_last_error())
    assert lib.fastmem_is_alive(proc)
    lib.fastmem_close(proc)
    ok("FM_ALL_ACCESS opens a process")


def check_grouped(lib, proc, base):
    bad = 0x1000

    # A failed window used to leave the caller's mask bytes untouched, so
    # a pre-filled mask reported success for addresses that never read.
    addrs = [base, bad, base + 8]
    mask = mask_array(3, 0xAA)
    out = (ctypes.c_char * 24)()
    got = lib.fastmem_read_grouped(proc, addr_array(addrs), 3, 8, 16,
                                   out, mask)
    assert got == 2, got
    assert list(mask) == [1, 0, 1], list(mask)
    assert bytes(out[:8]) == b"\x42" * 8
    assert bytes(out[16:24]) == b"\x42" * 8
    assert lib.fastmem_last_error() != 0, "a failed window set no error"
    ok("failed grouped windows zero their mask and set the error")

    # A wholly failed window, same contract.
    addrs = [bad, bad + 8]
    mask = mask_array(2, 0xAA)
    got = lib.fastmem_read_grouped(proc, addr_array(addrs), 2, 8, 16,
                                   out, mask)
    assert got == 0, got
    assert list(mask) == [0, 0], list(mask)
    assert lib.fastmem_last_error() != 0

    # A successful read afterwards must clear the stale error, or callers
    # cannot tell whether their last call failed.
    addrs = [base, base + 8]
    mask = mask_array(2, 0)
    got = lib.fastmem_read_grouped(proc, addr_array(addrs), 2, 8, 16,
                                   out, mask)
    assert got == 2
    assert list(mask) == [1, 1]
    assert lib.fastmem_last_error() == 0, lib.fastmem_last_error()
    ok("a successful grouped read clears the previous error")


def check_pool_init_race(lib, proc, base):
    # First pool use in this process, deliberately from several threads:
    # the one-time init used to be an unlocked ready flag.
    assert lib.fastmem_pool_workers() == 0
    addrs = addr_array([base + i * 8 for i in range(128)])
    barrier = threading.Barrier(4)
    errors = []

    def race():
        try:
            barrier.wait(10)
            for _ in range(10):
                out = (ctypes.c_char * (128 * 8))()
                mask = mask_array(128, 0)
                got = lib.fastmem_pool_read_many(proc, addrs, 128, 8,
                                                 out, mask, 2)
                if got != 128 or sum(mask) != 128:
                    errors.append((got, sum(mask)))
        except Exception as exc:                # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=race) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads), \
        "pool initialisation deadlocked"
    assert not errors, errors
    assert 1 <= lib.fastmem_pool_workers() <= 32
    ok("first-use pool initialisation survives concurrent callers")


def check_pool_counts(lib, proc, base):
    n = 200
    addrs = [base + i * 8 for i in range(n)]
    out = (ctypes.c_char * (n * 8))()
    mask = mask_array(n, 0)
    got = lib.fastmem_pool_read_many(proc, addr_array(addrs), n, 8,
                                     out, mask, 4)
    assert got == sum(mask) == n, (got, sum(mask))
    ok("pooled reads count every worker's successes")

    mixed = [base + i * 8 if i % 2 else 0x1000 for i in range(n)]
    out = (ctypes.c_char * (n * 8))()
    mask = mask_array(n, 0)
    got = lib.fastmem_pool_read_many(proc, addr_array(mixed), n, 8,
                                     out, mask, 4)
    assert got == sum(mask) == 100, (got, sum(mask))
    ok("mixed pooled reads count only the successes")


def check_pool_cap(lib, proc, base):
    n = 200
    addrs = addr_array([base + i * 8 for i in range(n)])
    for workers in (33, 64):
        out = (ctypes.c_char * (n * 8))()
        mask = mask_array(n, 0)
        got = lib.fastmem_pool_read_many(proc, addrs, n, 8, out, mask,
                                         workers)
        assert got == sum(mask) == n, (workers, got, sum(mask))
    live = lib.fastmem_pool_workers()
    assert 0 < live <= 32, live
    ok("worker requests above the pool cap are clamped ({})".format(live))

    lib.fastmem_pool_shutdown()
    assert lib.fastmem_pool_workers() == 0
    ok("pool shutdown joins its workers")


def main():
    print(SEP)
    print("FASTMEM native C library regressions")
    print(SEP)
    print("library: {}".format(DLL))

    if not os.path.isfile(DLL):
        print("[--] not built, run python tools/build_native.py first")
        print(SEP)
        return 0

    lib = load()
    pid = os.getpid()
    proc = lib.fastmem_open(pid)
    assert proc, "fastmem_open(self) failed with {}".format(
        lib.fastmem_last_error())

    # Keep the buffer alive for the whole run: the addresses point into it.
    buf = (ctypes.c_char * 4096)()
    ctypes.memset(buf, 0x42, 4096)
    base = ctypes.addressof(buf)

    try:
        check_access_mask(lib, pid)
        check_grouped(lib, proc, base)
        check_pool_init_race(lib, proc, base)
        check_pool_counts(lib, proc, base)
        check_pool_cap(lib, proc, base)
    finally:
        lib.fastmem_pool_shutdown()
        lib.fastmem_close(proc)

    print(SEP)
    print("All native test groups passed.")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
