"""Functional tests for fastmem.

Runs both with the C extension and without it (the fallback is verified
separately in CI).

Usage:
    python -m tests.test_fastmem
    pytest tests/
"""

import ctypes
import os
import random
import struct
import subprocess
import sys
import threading
import time

from fastmem import (
    FastMemError,
    Process,
    ProcessClosedError,
    ProcessOpenError,
    ProcessTerminatedError,
    ReadMemoryError,
    Region,
    backend,
)
from fastmem import _winapi as w

SEP = "=" * 74


def cbuf(data):
    """Create a ctypes buffer holding ``data``; return (buffer, address).

    The caller MUST keep the returned buffer alive for as long as the
    address is used. Dropping it (``_, addr = cbuf(...)``) lets the GC free
    the memory, the address becomes stale, and reads from it silently
    return whatever now occupies that memory.
    """
    buf = (ctypes.c_char * max(len(data), 1))()
    if data:
        ctypes.memmove(buf, data, len(data))
    return buf, ctypes.addressof(buf)


def cbuf_addr(data):
    """Like cbuf but for cases where the buffer is kept alive by a list.

    Returns (holder, address): put holder in a local or container so the
    buffer outlives the address.
    """
    buf, addr = cbuf(data)
    return [buf], addr


# Victim process code. It prints its OWN pid along with the buffer address.
#
# Why the pid rather than the one from subprocess: in a venv on Windows
# python.exe is a redirector that launches a child interpreter. The memory
# belongs to the child, and OpenProcess on the redirector opens a different
# address space (verified: the region at that address came back MEM_FREE).
VICTIM_CODE = """\
import ctypes, os, time
buf = (ctypes.c_char * (1 << 20))()
ctypes.memmove(buf, b'B' * (1 << 20), 1 << 20)
print(os.getpid(), ctypes.addressof(buf), flush=True)
time.sleep(120)
"""

VICTIM_LONG_CODE = """\
import ctypes, os, time
buf = (ctypes.c_char * (1 << 22))()
ctypes.memmove(buf, b'B' * (1 << 22), 1 << 22)
print(os.getpid(), ctypes.addressof(buf), flush=True)
time.sleep(240)
"""


def spawn_victim(code=VICTIM_CODE, delay=0.4):
    """Start a victim process. Returns (proc, pid, base_address)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    pid, base = (int(x) for x in proc.stdout.readline().split())
    time.sleep(delay)
    return proc, pid, base


def ok(label):
    print("[{}] OK".format(label))


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------

def test_compat():
    print(SEP)
    print("COMPATIBILITY")
    print(SEP)
    print("  page_size (GetSystemInfo): {} bytes".format(w.page_size()))
    print("  native machine: 0x{:X} ({})".format(
        w.native_machine(),
        w.MACHINE_NAMES.get(w.native_machine(), "?")))
    print("  IsWow64Process2 available: {} (Win10 1709+)".format(
        w.IsWow64Process2 is not None))
    print("  IsWow64Process available:  {}".format(w.IsWow64Process is not None))
    print("  Python bitness: {} bit".format(struct.calcsize("P") * 8))
    print("  backend: {} (C extension: {})".format(
        Process.backend(), backend.HAVE_C))
    if backend.HAVE_C:
        print("  extension version: {}".format(backend.extension_version()))

    with Process(os.getpid()) as p:
        assert p.pointer_size in (4, 8)
        assert p.is_64bit == (p.pointer_size == 8)
        assert p.page_size > 0
        print("  target: {} bit ({}), page_size={}".format(
            p.pointer_size * 8, p.machine_name, p.page_size))
    ok("target bitness detection")

    try:
        with Process(os.getpid(), access=w.PROCESS_VM_READ) as p2:
            assert p2.pointer_size in (4, 8)
        ok("opening with PROCESS_VM_READ only")
    except ProcessOpenError as exc:
        print("  PROCESS_VM_READ -> ProcessOpenError: {}".format(exc))

    big = b"\xAB" * 8
    _keep_big, addr = cbuf(big)
    with Process(os.getpid()) as p:
        if addr > 0xFFFFFFFF:
            assert p.read(addr, 8) == big, "address above 4 GB read wrong"
            ok("reading above 4 GB (0x{:X}) - pointers not truncated".format(addr))
        else:
            print("  test buffer below 4 GB, truncation check skipped")
    print()


# ---------------------------------------------------------------------------
# Single reads
# ---------------------------------------------------------------------------

def test_read():
    print(SEP)
    print("SINGLE READS")
    print(SEP)

    payload = struct.pack("<IfQd", 0xDEADBEEF, 3.5, 0x1122334455667788, 2.25)
    src, addr = cbuf(payload)

    with Process(os.getpid()) as p:
        assert p.pid == os.getpid()
        assert p.closed is False
        assert p.is_alive() is True
        print("  repr: {}".format(repr(p)))

        assert p.read(addr, len(payload)) == payload
        ok("read(24 bytes) matches the reference")

        assert p.read(addr, 8) == payload[:8]
        assert p.read(addr) == payload[:p.pointer_size]
        ok("read with default size = pointer_size ({})".format(p.pointer_size))

        assert p.read(addr, 8, or_none=True) == payload[:8]
        assert p.read(0x0, 8, or_none=True) is None
        assert p.read(0xDEADBEEF00, 8, or_none=True) is None
        ok("or_none=True returns None instead of raising")

        try:
            p.read(0x0, 8)
            raise AssertionError("expected an exception")
        except ReadMemoryError as exc:
            assert exc.address == 0 and exc.size == 8
            assert exc.code in (299, 87, 5, 6)
            print("  read(0, 8) -> {} (code={})".format(
                type(exc).__name__, exc.code))
        ok("read on a bad address raises ReadMemoryError")

        assert p.read(addr, as_="int") == 0xDEADBEEF
        assert p.read(addr, as_="i32") == -559038737
        assert abs(p.read(addr + 4, as_="float") - 3.5) < 1e-6
        assert p.read(addr + 8, as_="ptr") == 0x1122334455667788
        assert abs(p.read(addr + 16, as_="double") - 2.25) < 1e-9
        # low two bytes of 0xDEADBEEF are EF BE, little-endian -> 0xBEEF
        assert p.read(addr, as_="u16") == 0xBEEF
        ok("as_ interpretation: int/u16/i32/float/double/ptr")

        for bad in ("nonsense", "i33", ""):
            try:
                p.read(addr, as_=bad)
                raise AssertionError("expected ValueError")
            except ValueError:
                pass
        ok("unknown as_ raises ValueError")

        assert p.read(addr, 0) == b""
        ok("read(size=0) returns b''")
    print()


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_batch():
    print(SEP)
    print("BATCHING")
    print(SEP)

    payload = struct.pack("<IfQd", 0xDEADBEEF, 3.5, 0x1122334455667788, 2.25)
    src, addr = cbuf(payload)

    with Process(os.getpid()) as p:
        addrs = [addr + o for o in (0, 4, 8, 16)]
        exp = [payload[o:o + 8] for o in (0, 4, 8, 16)]

        assert p.read_many(addrs, 8) == exp
        ok("read_many matches the reference")

        mixed = p.read_many([addr, 0x0, addr + 8], 4)
        assert mixed[0] == payload[0:4]
        assert mixed[1] is None, "bad address must yield None"
        assert mixed[2] == payload[8:12]
        ok("mixed batch: None lands on the failing position")

        assert p.read_many([0x0, 0xDEAD0000], 8) == [None, None]
        assert p.read_many([], 8) == []
        assert p.read_many([addr], 0) == [None]
        ok("edge cases: empty, size=0, all bad")

        got = p.read_many((addr + i * 8 for i in range(3)), 8)
        assert got == [payload[i * 8:i * 8 + 8] for i in range(3)]
        ok("read_many accepts a generator")

        chunk = bytes(range(251)) + b"\x00" * 5
        _keep_chunk, caddr = cbuf(chunk)
        assert p.read_many([caddr, caddr], len(chunk)) == [chunk, chunk]
        ok("read_many(size={} > SCRATCH_SIZE)".format(len(chunk)))

        into = p.read_many([addr, 0x0, addr + 8], 4, into=True)
        assert isinstance(into, bytearray) and len(into) == 12
        assert into[0:4] == payload[0:4]
        assert into[4:8] == b"\x00" * 4, "failed address must read as zeros"
        assert into[8:12] == payload[8:12]
        ok("into=True returns one bytearray, failures are zeros")

        assert p.read_many([], 8, into=True) == bytearray()

        vals = p.read_many([addr + 8, 0x0, addr], as_="ptr")
        assert vals[0] == struct.unpack("<Q", payload[8:16])[0]
        assert vals[1] is None
        assert vals[2] == struct.unpack("<Q", payload[0:8])[0]
        ok("as_='ptr' produces a list of ints")

        assert p.read_many([addr], as_="int") == [0xDEADBEEF]
        ok("as_='int' produces unsigned values")

        # grouping
        clus = bytes(range(256)) * 16
        _keep_clus, base = cbuf(clus)
        clustered = [base + i * 16 for i in range(200)]
        expect = [clus[i * 16:i * 16 + 4] for i in range(200)]

        assert p.read_many(clustered, 4) == expect
        assert p.read_many(clustered, 4, span=0) == expect     # no grouping
        assert p.read_many(clustered, 4, span=4) == expect     # span = size
        assert p.read_many(clustered, 4, span=1 << 16) == expect
        ok("span grouping matches ungrouped results")

        for seed in range(20):
            sh = list(clustered)
            random.Random(seed).shuffle(sh)
            exp = [clus[(a - base) // 16 * 16:(a - base) // 16 * 16 + 4]
                   for a in sh]
            got = p.read_many(sh, 4)
            assert got == exp, "order broken at seed={}".format(seed)
        ok("result order preserved on 20 shuffled layouts")
    print()


# ---------------------------------------------------------------------------
# Regions and search
# ---------------------------------------------------------------------------

def test_regions():
    print(SEP)
    print("REGIONS AND SEARCH")
    print(SEP)

    needle = struct.pack("<Q", 0x1122334455667788)
    field = bytes(256) + needle + bytes(256) + needle + bytes(256)
    _keep_field, faddr = cbuf(field)
    freg = Region(faddr, len(field), w.MEM_COMMIT, w.PAGE_READWRITE,
                  w.MEM_PRIVATE)

    with Process(os.getpid()) as p:
        assert p.read_region(faddr, len(field)) == field
        assert p.read_region(faddr, len(field), or_none=True) == field
        assert p.read_region(0x0, 16, or_none=True) is None
        try:
            p.read_region(faddr, 1 << 30)
            raise AssertionError("expected ReadMemoryError")
        except ReadMemoryError:
            pass
        ok("read_region: exact, or_none, oversized raises")

        # field is 784 bytes, so a 256-byte chunking gives 256,256,256,16
        chunks = list(p.read_region(faddr, len(field), chunks=256))
        assert b"".join(chunks) == field
        assert all(len(c) <= 256 for c in chunks)
        assert sum(len(c) for c in chunks) == len(field)
        ok("chunks=256 yields {} blocks that reassemble".format(
            len(chunks)))

        # exactly divisible: every block must be full size
        clus_data = bytes(range(256)) * 8
        _keep_clusdata, clus_addr = cbuf(clus_data)
        even = list(p.read_region(clus_addr, len(clus_data), chunks=1024))
        assert b"".join(even) == clus_data
        assert all(len(c) == 1024 for c in even)
        ok("chunks=1024 over {} bytes gives full blocks".format(
            len(clus_data)))

        big_reg = max(p.regions(min_size=1 << 20), key=lambda r: r.size)
        assert len(p.read_region(big_reg.base, 1 << 20)) == (1 << 20)
        ok("read_region(1 MiB) from a real region")

        # search
        hits = list(p.find(needle, regions=[freg]))
        assert hits == [faddr + 256, faddr + 520], hits
        got = p.read(hits[0], 8)
        assert got == needle, "read at hit: {!r} != {!r}".format(got, needle)
        ok("find: {} hits at exact addresses".format(len(hits)))

        assert list(p.find(needle, regions=[freg], chunk_size=256)) == hits
        ok("chunk_size=256 gives the same result")

        straddle = b"\x00" * 200 + needle + b"\x00" * 200
        _keep_straddle, saddr = cbuf(straddle)
        sreg = Region(saddr, len(straddle), w.MEM_COMMIT,
                      w.PAGE_READWRITE, w.MEM_PRIVATE)
        for cs in (64, 128, 256, 512):
            assert list(p.find(needle, regions=[sreg], chunk_size=cs)) \
                == [saddr + 200], cs
        ok("hit straddling a block boundary found at chunk 64/128/256/512")

        aln = (b"\x00" * 3 + b"\xAB\xAB\xAB\xAB") * 40
        _keep_aln, aaddr = cbuf(aln)
        areg = Region(aaddr, len(aln), w.MEM_COMMIT, w.PAGE_READWRITE,
                      w.MEM_PRIVATE)
        exp_all = [aaddr + i * 7 + 3 for i in range(40)]
        exp_aln = [a for a in exp_all if a % 4 == 0]
        assert list(p.find(b"\xAB\xAB\xAB\xAB", regions=[areg])) == exp_all
        assert list(p.find(b"\xAB\xAB\xAB\xAB", regions=[areg], align=4)) \
            == exp_aln
        ok("align=4 keeps {} of {} hits".format(len(exp_aln), len(exp_all)))

        assert list(p.find(b"", regions=[freg])) == []
        assert list(p.find(needle, regions=[])) == []
        assert len(list(p.find(b"\xAB\xAB\xAB\xAB", regions=[areg],
                               limit=7))) == 7
        ok("edge cases: empty pattern, empty regions, limit")

        # enumeration
        regs = list(p.regions())
        assert regs, "no regions found"
        assert all(r.size > 0 for r in regs)
        assert all(a.base <= b.base for a, b in zip(regs, regs[1:])), \
            "regions must be ordered by address"
        readable = list(p.regions(min_size=0x1000))
        assert readable and all(r.committed and r.readable for r in readable)
        capped = list(p.regions(max_size=0x100000))
        assert all(r.size <= 0x100000 for r in capped)
        print("  {} regions, {} readable, {} capped".format(
            len(regs), len(readable), len(capped)))
        ok("regions(): enumeration, filters and truncation")

        first = readable[0]
        assert p.read_region(first.base, min(first.size, 0x1000)) is not None
        ok("first readable region 0x{:X} is readable".format(first.base))

        reg = p.query(0x1000)
        assert reg is not None and reg.base <= 0x1000 < reg.end
        assert reg.contains(0x1000) is True
        assert p.query(0xFFFFFFFFFFFFFFFF) is None
        ok("query(): region by address, None outside the address space")
    print()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_errors():
    print(SEP)
    print("ERROR HANDLING")
    print(SEP)

    for pid in (0x7FFFFFF0, 0, -1):
        try:
            Process(pid)
            print("  PID {} opened (possibly in use)".format(pid))
        except ProcessOpenError as exc:
            assert isinstance(exc.code, int)
            print("  PID {:<12} -> ProcessOpenError, code={}".format(
                pid, exc.code))
    ok("nonexistent PIDs raise ProcessOpenError")

    try:
        with Process(4) as sysp:
            print("  PID 4 (System) opened, {} bit".format(
                sysp.pointer_size * 8))
    except FastMemError as exc:
        print("  PID 4 (System) -> {}: expected without admin rights".format(
            type(exc).__name__))
    ok("protected process handled without crashing")

    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(30)"])
    time.sleep(0.4)
    try:
        with Process(proc.pid) as vp:
            assert vp.is_alive()
            stop = {"go": True}
            seen = [0]
            caught = []

            def reader():
                while stop["go"]:
                    try:
                        if vp.read(0x1000, 8, or_none=True) is not None:
                            seen[0] += 1
                    except Exception as exc:      # any exception is a bug
                        caught.append(exc)
                        return

            th = threading.Thread(target=reader, daemon=True)
            th.start()
            time.sleep(0.05)
            proc.kill()
            th.join(timeout=10)
            stop["go"] = False
            assert not caught, "read raised: {!r}".format(caught)
            print("  reading during kill(): no exceptions, {} hits".format(
                seen[0]))
            try:
                vp.read(0x1000, 8)
                print("  read() after death: unexpected success")
            except FastMemError as exc:
                print("  read() after death -> {} (FastMemError)".format(
                    type(exc).__name__))
    except ProcessOpenError as exc:
        print("  victim died before opening: {}".format(exc))
    finally:
        proc.kill()
        proc.wait()
    ok("process dying mid-read does not raise from or_none")

    p2 = Process(os.getpid())
    p2.close()
    p2.close()                                # idempotent
    assert p2.closed and p2.handle is None
    assert p2.is_alive() is False
    for call in (lambda: p2.read(0x1000, 8),
                 lambda: p2.read_many([0x1000], 8),
                 lambda: p2.read_region(0x1000, 8),
                 lambda: list(p2.regions())):
        try:
            call()
            raise AssertionError("expected an exception")
        except ProcessClosedError as exc:
            assert isinstance(exc, ReadMemoryError), \
                "ProcessClosedError must be a ReadMemoryError"
    ok("close() idempotent; use after close -> ProcessClosedError")

    err = ReadMemoryError(0x1000, 8, 299)
    assert err.address == 0x1000 and err.size == 8 and err.code == 299
    assert "0x1000" in str(err)
    assert "1234" in str(ProcessOpenError(1234, 5))
    assert ProcessTerminatedError(0x1000, 8, 6).code == 6
    ok("exception attributes and lazily formatted messages")
    print()


# ---------------------------------------------------------------------------
# Foreign process
# ---------------------------------------------------------------------------

def test_foreign():
    print(SEP)
    print("FOREIGN PROCESS")
    print(SEP)

    proc, pid, base = spawn_victim()
    try:
        with Process(pid) as p:
            print("  PID {} opened, {} bit".format(pid, p.pointer_size * 8))
            want = b"B" * 8

            addrs = [base + i * 8 for i in range(100)]
            got = p.read_many(addrs, 8)
            assert len(got) == 100
            assert all(g == want for g in got), "data mismatch"
            ok("100 addresses from a foreign process all correct")

            assert bytes(p.read_many(addrs, 8, into=True)) == want * 100
            ok("into=True on a foreign process")

            mixed = p.read_many([base, 0x0, base + 16], 8)
            assert mixed[0] is not None and mixed[1] is None
            assert mixed[2] is not None
            ok("mixed batch on a foreign process")

            cluster = [base + i * 64 for i in range(200)]
            assert p.read_many(cluster, 8) == [want] * 200
            assert p.read_region(base, 4096) == b"B" * 4096
            ok("grouping and read_region on a foreign process")
    finally:
        proc.kill()
        proc.wait()
    print()


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def test_threads():
    print(SEP)
    print("THREADS")
    print(SEP)

    proc, pid, base = spawn_victim(VICTIM_LONG_CODE)
    try:
        with Process(pid) as p:
            addrs = [base + i * 8 for i in range(2000)]

            if not backend.HAVE_C:
                # Both shapes must refuse: into=True took a different route
                # and used to fall through to the single-threaded path.
                for kwargs in ({}, {"into": True}):
                    try:
                        p.read_many(addrs, 8, threads=4, **kwargs)
                        raise AssertionError(
                            "expected RuntimeError without C, kwargs={}"
                            .format(kwargs))
                    except RuntimeError:
                        pass
                # threads=1 is not a request for parallelism, so it works.
                assert len(p.read_many(addrs[:16], 8, threads=1)) == 16
                ok("without C, threads>1 raises RuntimeError on both paths")
                return

            one = p.read_many(addrs, 8)
            assert all(b is not None for b in one)
            for nt in (2, 4):
                assert p.read_many(addrs, 8, threads=nt) == one, \
                    "thread count changed the result"
            ok("1/2/4 threads give identical results")

            # Chunk boundaries must tile the range exactly: a gap leaves a
            # hole in the mask, an overlap double-reads and desynchronises
            # the completion count so the caller returns mid-write.
            shapes = {
                "exact multiple": [base + i * 8 for i in range(2048)],
                "odd count": [base + i * 8 for i in range(1999)],
                "1 address": [base],
                "2 addresses": [base, base + 8],
                "3 addresses": [base, base + 8, base + 16],
                "fewer than workers": [base, base + 8, base + 16],
            }
            for label, sub in shapes.items():
                expect = p.read_many(sub, 8, span=0)
                for nt in (1, 2, 3, 5, 8):
                    got = p.read_many(sub, 8, threads=nt, span=0)
                    assert got == expect, \
                        "{} with {} workers: {!r} != {!r}".format(
                            label, nt, got, expect)
                # Mixed readable and unreadable addresses in one batch.
                mixed = [base + i * 8 if i % 3 else 0x1000
                         for i in range(len(sub))]
                expect = p.read_many(mixed, 8, span=0)
                for nt in (2, 4):
                    got = p.read_many(mixed, 8, threads=nt, span=0)
                    assert got == expect, \
                        "{} mixed with {} workers".format(label, nt)
            ok("chunk shapes tile the batch exactly (6 shapes x 5 worker "
               "counts)")

            # A mask with holes must not bleed into the next call, and the
            # data must actually land in the buffer.
            holes = [base + i * 8 if i % 2 else 0x1000 for i in range(1000)]
            into_single = p.read_many(holes, 8, into=True, span=0)
            for nt in (2, 4, 8):
                assert p.read_many(holes, 8, into=True, span=0,
                                   threads=nt) == into_single, \
                    "into=True changed with {} workers".format(nt)
            ok("into=True matches single-threaded, including failures")

            # Out-of-range addresses must read as failures, not raise: a
            # scanner sweeping uninitialised memory hits them constantly.
            garbage = [base, 2 ** 70, -1, 0xFFFFFFFFFFFFFFFF, base + 8]
            expect = p.read_many(garbage, 8, span=0)
            assert expect[0] is not None and expect[4] is not None, expect
            assert expect[1:4] == [None, None, None], expect
            for nt in (2, 4):
                assert p.read_many(garbage, 8, threads=nt, span=0) == expect
            ok("out-of-range addresses fail instead of raising")

            # The pool must be reused: rebuilding workers per call is what
            # made threads slower than no threads at all.
            backend.pool_shutdown()
            assert backend.pool_workers() == 0
            for _ in range(5):
                p.read_many(addrs, 8, threads=4, span=0)
            assert backend.pool_workers() == 4, \
                "expected 4 live workers, got {}".format(
                    backend.pool_workers())
            p.read_many(addrs, 8, threads=2, span=0)
            assert backend.pool_workers() == 4, \
                "a smaller request must not shrink the pool"
            ok("worker threads are created once and reused")

            # A worker left mid-job when a read returns would show up as a
            # corrupted later read, so repeat and compare each time against
            # the single-threaded result.
            noisy = [base + i * 8 if i % 5 else 0x1000 for i in range(3000)]
            expect = p.read_many(noisy, 8, span=0)
            for round_ in range(30):
                got = p.read_many(noisy, 8, threads=4, span=0)
                assert got == expect, \
                    "read {} diverged at index {}".format(
                        round_, next((i for i, (a, b) in
                                      enumerate(zip(expect, got)) if a != b),
                                     None))
            ok("30 repeated batched reads stay consistent")

            assert p.read_many([], 8, threads=4) == []
            assert p.read_many(addrs[:5], 8, threads=10) is not None
            ok("edges: empty input, threads > address count")

            # Shutdown must drain, otherwise the process would exit with
            # threads parked in a wait.
            backend.pool_shutdown()
            assert backend.pool_workers() == 0
            # Usable again afterwards: the pool rebuilds on demand.
            assert p.read_many(addrs[:64], 8, threads=4, span=0) is not None
            ok("pool shuts down and rebuilds on demand")
    finally:
        proc.kill()
        proc.wait()
    print()


def test_sources():
    """The package's own source files must be clean UTF-8 without a BOM.

    Not about behaviour, but a BOM or a mangled character in a shipped
    source file breaks imports on some paths and hides review of the code
    that reads it. Both happened while editing these files through a
    PowerShell round-trip on a non-UTF8 console code page.
    """
    print(SEP)
    print("SOURCES")
    print(SEP)

    import fastmem

    root = os.path.dirname(os.path.abspath(fastmem.__file__))
    checked = 0
    for name in sorted(os.listdir(root)):
        if not name.endswith((".py", ".c")):
            continue
        path = os.path.join(root, name)
        raw = open(path, "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf"), \
            "{} starts with a UTF-8 BOM".format(name)
        text = raw.decode("utf-8")   # raises on invalid bytes
        # Mojibake: the UTF-8 bytes of a box-drawing char decoded twice,
        # or the replacement character left behind by a lossy write.
        assert "\ufffd" not in text, "{} contains a replacement character".format(name)
        assert "\u00c3" not in text, "{} contains double-encoded text".format(name)
        checked += 1
    ok("{} package source files are clean UTF-8, no BOM, no mojibake".format(
        checked))
    print()


# ---------------------------------------------------------------------------

TESTS = [
    test_compat,
    test_read,
    test_batch,
    test_regions,
    test_errors,
    test_foreign,
    test_threads,
    test_sources,
]


def main():
    import fastmem

    print(SEP)
    print("FASTMEM {} functional tests".format(fastmem.__version__))
    print("Python {}.{}  |  backend: {}".format(
        sys.version_info.major, sys.version_info.minor, Process.backend()))
    print(SEP)

    failed = []
    for fn in TESTS:
        try:
            fn()
        except Exception as exc:                    # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed.append((fn.__name__, exc))
            print("[!!] {} FAILED: {!r}\n".format(fn.__name__, exc))

    print(SEP)
    if failed:
        print("{} of {} test groups FAILED:".format(len(failed), len(TESTS)))
        for name, exc in failed:
            print("  - {}: {!r}".format(name, exc))
        print(SEP)
        return 1
    print("All {} test groups passed.".format(len(TESTS)))
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
