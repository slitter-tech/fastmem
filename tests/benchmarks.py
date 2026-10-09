"""Benchmarks for fastmem.

Usage:
    python -m tests.benchmarks            # own process (fast)
    python -m tests.benchmarks --foreign  # foreign process (realistic)

Compares the pure Python reference paths against the C implementations.
That distinction matters: with the extension present, read_many_into and
read_many already go through C, so comparing them against "pure Python"
without bypassing C would measure C against C.
"""

import ctypes
import os
import struct
import subprocess
import sys
import time

from fastmem import Process, backend

SEP = "=" * 74

# Baseline before any optimisation, as originally reported
BASELINE_US = 9.3


def bench(fn, n, repeats=5):
    """Warm up, then time n calls. Returns microseconds per call."""
    fn()
    best = None
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(n):
            fn()
        per_call = (time.perf_counter() - start) / n * 1e6
        if best is None or per_call < best:
            best = per_call
    return best


def cbuf(data):
    buf = (ctypes.c_char * max(len(data), 1))()
    if data:
        ctypes.memmove(buf, data, len(data))
    return buf, ctypes.addressof(buf)


def spawn_victim():
    # Prints its own pid: in a venv on Windows python.exe is a redirector
    # that launches a child interpreter, and the memory belongs to the child.
    code = ("import ctypes,os,time\n"
            "b=(ctypes.c_char*(1<<22))()\n"
            "ctypes.memmove(b,b'B'*(1<<22),1<<22)\n"
            "print(os.getpid(),ctypes.addressof(b),flush=True)\n"
            "time.sleep(240)\n")
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    pid, base = (int(x) for x in proc.stdout.readline().split())
    time.sleep(0.4)
    return proc, pid, base


def section(title):
    print()
    print(SEP)
    print(title)
    print(SEP)


def row(label, value, extra=""):
    print("{:<40} {:12.3f} {}".format(label, value, extra))


# ---------------------------------------------------------------------------
# Reference implementations, bypassing the C extension entirely.
#
# These live in the benchmark rather than in the library on purpose: the
# public API stays small, but measuring "pure Python" against a method that
# already routes through C would compare C with C.
# ---------------------------------------------------------------------------

def python_batch_into(p, addrs, size):
    """Pure Python batch into a bytearray."""
    from fastmem import _winapi as w

    rpm = w.ReadProcessMemory
    dst = bytearray(len(addrs) * size)
    view = (ctypes.c_char * len(dst)).from_buffer(dst)
    offset = 0
    for addr in addrs:
        rpm(p.handle, addr, ctypes.byref(view, offset), size, None)
        offset += size
    return dst


def python_batch_bytes(p, addrs, size):
    """Pure Python batch into a list of bytes."""
    from fastmem import _winapi as w

    rpm = w.ReadProcessMemory
    string_at = ctypes.string_at
    scratch = ctypes.create_string_buffer(size)
    addr_scratch = ctypes.addressof(scratch)
    out = []
    append = out.append
    for addr in addrs:
        if rpm(p.handle, addr, addr_scratch, size, None):
            append(string_at(addr_scratch, size))
        else:
            append(None)
    return out


# ---------------------------------------------------------------------------

def bench_single(p, good, n=10000):
    section("SINGLE READS (N={})".format(n))
    print("{:<40} {:>10} {:>11}".format("operation", "us", "vs baseline"))
    print("-" * 66)

    def swallow(fn, *args):
        from fastmem import FastMemError
        try:
            return fn(*args)
        except FastMemError:
            return None

    rows = [
        ("read() in try/except, bad address",
         lambda: swallow(p.read, 0x0, 8)),
        ("read(or_none=True), bad address",
         lambda: p.read(0x0, 8, or_none=True)),
        ("read() success, 8 bytes", lambda: p.read(good, 8)),
        ("read(or_none=True) success, 8 bytes",
         lambda: p.read(good, 8, or_none=True)),
        ("read(as_='int')", lambda: p.read(good, as_="int")),
        ("read(as_='float')", lambda: p.read(good, as_="float")),
        ("read(as_='ptr')", lambda: p.read(good, as_="ptr")),
    ]
    for label, fn in rows:
        us = bench(fn, n)
        print("{:<40} {:10.2f} {:10.1f}x".format(label, us, BASELINE_US / us))


def bench_batch(p, addrs, rounds=20):
    section("BATCHES ({} addresses, 8 bytes each)".format(len(addrs)))
    print("{:<40} {:>12} {:>12}".format(
        "operation", "us/address", "speedup"))
    print("-" * 68)

    cnt = len(addrs)
    py_into = bench(lambda: python_batch_into(p, addrs, 8), rounds) / cnt
    c_into = bench(lambda: p.read_many(addrs, 8, into=True), rounds) / cnt
    py_list = bench(lambda: python_batch_bytes(p, addrs, 8), rounds) / cnt
    c_list = bench(lambda: p.read_many(addrs, 8, span=0), rounds) / cnt
    py_ptr = bench(lambda: python_batch_bytes(p, addrs, 8), rounds) / cnt
    c_ptr = bench(lambda: p.read_many(addrs, as_="ptr", span=0),
                  rounds) / cnt

    row("Python -> bytearray", py_into)
    row("C -> bytearray", c_into, "{:.2f}x".format(py_into / c_into))
    row("Python -> list[bytes]", py_list)
    row("C -> list[bytes]", c_list, "{:.2f}x".format(py_list / c_list))
    row("Python -> list[bytes] (int unpack)", py_ptr)
    row("C -> list[int]", c_ptr, "{:.2f}x".format(py_ptr / c_ptr))
    print()
    print("  Note: read_many() with default span groups nearby addresses,")
    print("  so for dense input it is faster than span=0.")


def bench_grouping(p, cluster, rounds=20):
    section("GROUPING ({} addresses, 64 byte stride)".format(len(cluster)))
    print("{:<40} {:>12} {:>12}".format(
        "operation", "us/address", "speedup"))
    print("-" * 68)

    cnt = len(cluster)
    stream = bench(lambda: python_batch_bytes(p, cluster, 8), rounds) / cnt
    py_grp = bench(lambda: p.read_many(cluster, 8, span=4096),
                   rounds) / cnt
    c_grp = bench(lambda: p.read_many(cluster, 8, span=1 << 16), rounds) / cnt

    row("Python streaming", stream)
    row("C grouping (4 KiB)", py_grp, "{:.2f}x".format(stream / py_grp))
    row("C grouping (64 KiB)", c_grp, "{:.2f}x".format(stream / c_grp))
    print()
    print("  C grouping vs Python streaming: {:.1f}x".format(stream / c_grp))


def bench_region(p, base, rounds=300):
    section("LARGE BLOCKS (one API call each)")
    # 1 MiB comes from a genuinely mapped region: a test buffer may be
    # smaller and the read would hit unmapped memory.
    big_reg = next(iter(p.regions(min_size=1 << 21)), None)
    big_base = big_reg.base if big_reg else base

    print("{:<40} {:>12} {:>14}".format("operation", "us", "MB/s"))
    print("-" * 70)
    for size in (4096, 65536, 1 << 20):
        src = big_base if size >= (1 << 20) else base
        us = bench(lambda s=size, a=src: p.read_region(a, s), rounds,
                   repeats=3)
        row("read_region({:,} bytes)".format(size), us,
            "{:.0f} MB/s".format(size / us))


def bench_threads(p, addrs, rounds=15):
    section("THREADS")
    if not backend.HAVE_C:
        print("[skipped] requires the C extension")
        return

    import threading

    cnt = len(addrs)
    ncpu = os.cpu_count() or 1

    def py_threads(nt):
        """Pure Python across nt threads - demonstrates the GIL penalty."""
        chunk = (cnt + nt - 1) // nt
        parts = [addrs[i:i + chunk] for i in range(0, cnt, chunk)]

        def work():
            for sub in parts:
                p.read_many(sub, 8, into=True, span=0)

        ths = [threading.Thread(target=work) for _ in range(nt)]
        start = time.perf_counter()
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        return time.perf_counter() - start

    print("{:<40} {:>12} {:>10}".format("variant", "us/address", "speedup"))
    print("-" * 66)

    t1 = bench(lambda: p.read_many(addrs, 8, threads=1, span=0),
               rounds) / cnt
    row("C, 1 thread, no grouping", t1)
    for nt in (2, 4):
        if nt > ncpu:
            continue
        tn = bench(lambda n=nt: p.read_many(addrs, 8, threads=n, span=0),
                   rounds) / cnt
        row("C, {} threads, no grouping".format(nt), tn,
            "{:.2f}x".format(t1 / tn))
    print()
    print("Same through the into=True path (one bytearray, no per-address")
    print("Python object):")
    ti1 = bench(lambda: p.read_many(addrs, 8, into=True, span=0),
                rounds) / cnt
    row("C, 1 thread, into bytearray", ti1)
    for nt in (2, 4):
        if nt > ncpu:
            continue
        tn = bench(lambda n=nt: p.read_many(addrs, 8, into=True, span=0,
                                           threads=n),
                   rounds) / cnt
        row("C, {} threads, into bytearray".format(nt), tn,
            "{:.2f}x".format(ti1 / tn))
    print()
    print("Threads earn their keep on sparse addresses, where grouping")
    print("cannot merge anything:")
    sparse = [a for a in addrs[::16]]
    ts = bench(lambda: p.read_many(sparse, 8, threads=1, span=0),
               rounds) / len(sparse)
    row("C, sparse, 1 thread", ts)
    for nt in (2, 4):
        if nt > ncpu:
            continue
        tn = bench(lambda n=nt: p.read_many(sparse, 8, threads=n, span=0),
                   rounds) / len(sparse)
        row("C, sparse, {} threads".format(nt), tn,
            "{:.2f}x".format(ts / tn))
    print()
    print("Worker threads are created once and reused. Rebuilding them per")
    print("call cost ~812 us, which is why this used to measure 1.11-1.16x,")
    print("and 0.20x on sparse input.")
    print("  live workers: {}".format(backend.pool_workers()))
    print()
    print("For comparison, pure Python across threads gains far less:")
    py1 = bench(lambda: python_batch_into(p, addrs, 8), rounds) / cnt
    py_n = bench(lambda: py_threads(4), rounds) / cnt
    row("Python, 1 thread", py1)
    row("Python, 4 threads", py_n, "{:.2f}x".format(py1 / py_n))


# ---------------------------------------------------------------------------

def main():
    foreign = "--foreign" in sys.argv

    print(SEP)
    print("FASTMEM BENCHMARKS")
    print(SEP)
    print("Python {}.{}  |  {} cores  |  backend: {}".format(
        sys.version_info.major, sys.version_info.minor,
        os.cpu_count(), Process.backend()))
    print("Mode: {}".format(
        "foreign process (realistic)" if foreign else "own process (fast)"))
    print("Baseline before optimisation: {:.1f} us per call".format(
        BASELINE_US))

    if not foreign:
        with Process(os.getpid()) as p:
            _, good = cbuf(b"12345678")
            addrs = [good + i * 8 for i in range(1000)]
            _, cluster_base = cbuf(b"B" * 65536)
            cluster = [cluster_base + i * 64 for i in range(1000)]

            bench_single(p, good)
            bench_batch(p, addrs)
            bench_grouping(p, cluster)
            bench_region(p, cluster_base)
            bench_threads(p, addrs)
        return 0

    proc, pid, base = spawn_victim()
    try:
        with Process(pid) as p:
            addrs = [base + i * 8 for i in range(2000)]
            cluster = [base + i * 64 for i in range(2000)]

            bench_single(p, base)
            bench_batch(p, addrs)
            bench_grouping(p, cluster)
            bench_region(p, base)
            bench_threads(p, addrs)
    finally:
        proc.kill()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
