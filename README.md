[Русский](README_RU.md) | **English**

# fastmem

[![PyPI](https://img.shields.io/pypi/v/fastmem.svg)](https://pypi.org/project/fastmem/)
[![Python](https://img.shields.io/pypi/pyversions/fastmem.svg)](https://pypi.org/project/fastmem/)
[![CI](https://github.com/slitter-tech/fastmem/actions/workflows/ci.yml/badge.svg)](https://github.com/slitter-tech/fastmem/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

High-performance process memory reading for Windows. Pure `ctypes` and the
standard library. An optional C extension is built automatically when a
compiler is available, but is never required.

**You do not need a compiler.** Wheels for every supported Python and
architecture are built on GitHub and uploaded to PyPI, so `pip install
fastmem` normally installs a ready binary. A compiler is needed only if you
install from the sdist, and even then the library still works without one.

The same engine also ships as a standalone C library and a header-only C++
wrapper - see [docs/C_API.md](docs/C_API.md).

Built for reverse engineering, debugging, memory forensics and security
research.

## Installation

```bash
pip install fastmem
```

From source, with a local C extension build:

```bash
git clone https://github.com/slitter-tech/fastmem.git
cd fastmem
pip install -e .
```

## Quick start

```python
from fastmem import Process

with Process(pid) as p:
    print(p.machine_name, p.pointer_size * 8, "bit")
    data = p.read(address, 32)              # bytes
    value = p.read(address, as_="float")    # number
    obj = p.read(address)                   # pointer_size bytes
```

Hot loops use `or_none=True`, which returns `None` instead of raising:

```python
hit = p.read(address, 8, or_none=True)
```

## API

Eight methods cover everything.

| Method | Result | Use for |
|---|---|---|
| `read(addr, size=None, as_=None, or_none=False)` | `bytes`, number or `None` | single reads |
| `read_many(addrs, size=None, as_=None, into=False, span=-1, threads=0)` | `list` or `bytearray` | **batches** |
| `read_region(base, size, chunks=0, or_none=False)` | `bytes` or generator | **scanners** |
| `regions(min_size=0, max_size=0, ...)` | generator of `Region` | virtual memory walk |
| `find(pattern, regions=None, align=1, limit=0, chunk_size=0)` | generator of addresses | **value search** |
| `query(addr)` | `Region` or `None` | describe one region |
| `is_alive()` | `bool` | liveness |
| `close()` | - | release the handle |

### Reading

`size=None` means the target pointer size, so `read(addr)` reads a pointer
directly. `as_` interprets the bytes as a number:

| `as_` | Meaning |
|---|---|
| `None` | raw `bytes` |
| `'ptr'` | pointer, following the **target** bitness (8 on x64/ARM64, 4 on x86) |
| `'int'`, `'uint'` | unsigned 32-bit |
| `'i32'`, `'u32'` | signed / unsigned 32-bit |
| `'i64'`, `'u64'` | signed / unsigned 64-bit |
| `'i8'`, `'u8'`, `'i16'`, `'u16'` | narrow integers |
| `'float'`, `'f32'` | 32-bit float |
| `'double'`, `'f64'` | 64-bit float |
| `'long'`, `'ulong'` | unsigned 64-bit |

`int` and `long` are unsigned on purpose: memory reads are about raw values
(hashes, flags, pointers), and `0xDEADBEEF` coming back as `-559038737`
surprises everyone. Use `i32` / `i64` when you want signed interpretation.

```python
with Process(pid) as p:
    hp = p.read(addr, as_="float")
    n = p.read(addr, as_="int")
    ok = p.read(addr, as_="ptr", or_none=True)
```

### Batches

```python
with Process(pid) as p:
    values = p.read_many(addrs, as_="ptr")   # list[int | None]
    raw = p.read_many(addrs, into=True)      # one bytearray, failures zeroed
```

`span` groups addresses closer than `span` bytes and reads each group with
a single call. `-1` (default) uses four pages, `0` disables grouping.
Nearby addresses are the common case in real work - object fields, array
elements, list nodes - and grouping is up to **67x** faster than reading
them one at a time.

`threads` spreads the addresses over worker threads and requires the C
extension. The workers live in C, are created on first use and are reused
afterwards, so the cost is paid once rather than per call. Measured gain is
**1.5x at 2 threads and 2.6x at 4** on addresses too spread out for
grouping; grouping still wins whenever it applies, so pair `threads` with
`span=0` when the addresses are scattered.

### Regions

```python
with Process(pid) as p:
    for reg in p.regions(max_size=64 << 20):
        blob = p.read_region(reg.base, reg.size, or_none=True)
        if blob:
            scan(blob)

    # stream a large region instead of holding it in memory
    for chunk in p.read_region(base, size, chunks=1 << 20):
        scan(chunk)
```

### Search

```python
import struct
from fastmem import Process

needle = struct.pack("<Q", 0x1A2B3C4D5E6F7788)

with Process(pid) as p:
    for addr in p.find(needle, align=8, limit=1000):
        print(hex(addr))
```

The search runs in C through `bytes.find`: locating a pattern inside a
megabyte region takes ~0.3 us, while a Python loop over the bytes is five
orders of magnitude slower. `chunk_size` reads in overlapping blocks so
hits straddling a boundary survive.

## Target properties

Detected automatically via `IsWow64Process2` → `IsWow64Process` → system
architecture, so `as_='ptr'` follows the target, not the host.

```python
p.pointer_size   # 4 or 8
p.is_64bit       # bool
p.machine_name   # 'x86', 'x64', 'ARM64', ...
p.page_size      # from GetSystemInfo
p.backend()      # 'c-extension' or 'python-ctypes'
```

## Examples

Walk a pointer chain:

```python
with Process(pid) as p:
    node = p.read(root, as_="ptr")
    chain = []
    while node and len(chain) < 100:
        chain.append(node)
        node = p.read(node, or_none=True)
        if node:
            node = int.from_bytes(node, "little")
    print("chain length:", len(chain))
```

Dump every readable region:

```python
with Process(pid) as p:
    total = 0
    for reg in p.regions(max_size=64 << 20):
        blob = p.read_region(reg.base, reg.size, or_none=True)
        if blob:
            total += len(blob)
    print("read", total, "bytes")
```

## Compatibility

* Windows 7, 8, 8.1, 10, 11, Server 2012+; x86, x64, ARM64.
* Python 3.9-3.13. PyPy works on pure Python.
* 32- and 64-bit targets, including WOW64.
* Protected processes (PPL) and PID 4 (System): insufficient rights give
  `ProcessOpenError` with readable text and a Windows error code, never a
  crash.
* A process dying mid-read: `or_none=True` yields `None`, plain `read`
  raises `ProcessTerminatedError` (a `ReadMemoryError` subclass).
* Minimum access rights by default:
  `PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION`, which works
  without elevation for processes of the same user. Override with
  `Process(pid, access=...)`.
* No hardcoded Windows structure offsets and no assembly: only public APIs,
  each checked for availability.
* Page sizes are read from `GetSystemInfo`, never assumed.

## Thread safety

A `Process` instance is **not** thread-safe (it holds a reusable buffer).
Open one `Process` per thread for manual parallel reads. `read_many` is the
exception: the worker pool is shared, guarded, and rebuilt on demand, so it
is safe to call from several Python threads at once.

## Performance

Python 3.11, 4 cores, foreign process, 8 bytes per address. "Python" means
the same work through `ctypes` with the extension removed, so the columns
differ only in where the loop runs.

| Operation | Python | C | Speedup |
|---|---|---|---|
| `read()` in try/except, bad address | 10.46 | - | - |
| `read(or_none=True)`, bad address | 3.55 | - | - |
| `read()` success | 3.53 | - | - |
| `read(as_='int')` | 5.25 | - | - |
| batch into `bytearray` | 5.08 | 1.40 | 3.62x |
| batch into `list[bytes]` | 5.82 | 1.36 | 4.27x |
| batch into `list[int]` | 5.29 | 1.47 | 3.59x |
| **grouping vs streaming** | 5.12 | **0.076** | **67.2x** |

Large blocks, one API call each:

| | us | MB/s |
|---|---|---|
| `read_region(4 KiB)` | 9.7 | 421 |
| `read_region(64 KiB)` | 22.0 | 2977 |
| `read_region(1 MiB)` | 817 | 1284 |

### Where the speed comes from

1. **Block size matters more than language.** One `read_region(64 KiB)`
   costs ~22 us; reading the same 64 KiB address by address costs ~150 ms.
2. **Search belongs in C.** `bytes.find` beats a Python loop by five orders
   of magnitude.
3. **Allocation zeroes memory.** `(ctypes.c_char * n)()` memsets: ~400 us
   for 1 MiB. A reusable buffer removes it.
4. **Exceptions are expensive.** Formatting the error text cost 3.3 us; the
   message is now built lazily in `__str__`.
5. **C means one boundary crossing instead of a thousand.** A Python -> C
   call costs ~0.6 us, and a 1000-address batch in Python pays that a
   thousand times. The C loop crosses once.
6. **Handing data to C has a price too.** `(ctypes.c_size_t * n)(*addrs)`
   took 330 us for 2000 addresses; `array.array` fills the same buffer in
   66 us. Worth 12% of a threaded read on its own.

### Threads

Measured on a foreign process, no grouping, microseconds per address:

| | 1 thread | 2 | 4 |
|---|---|---|---|
| C, worker pool | 1.28 | 0.85 (1.51x) | 0.50 (2.57x) |
| same, into a bytearray | 1.26 | 0.96 (1.32x) | 0.54 (2.32x) |
| same, sparse addresses | 1.24 | 0.93 (1.33x) | 0.69 (1.80x) |

Threads used to be a net loss here: building `threading.Thread` objects per
call cost ~812 us on Windows, more than the ~1700 us of work it was meant
to parallelise, so four threads measured 1.11x on dense input and 0.20x on
sparse input - worse than not threading at all. The workers now live in C,
are created on first use and park on an event between calls, which is what
turned those into 2.57x and 1.80x. Repeated runs put the four-thread figure
between 2.57x and 2.71x; absolute timings move with machine load, the
ratios do not.

A control group confirms the mechanism: a variant that holds the GIL gains
exactly nothing from four threads (1617 us against 1605 us for one), so the
limiter is the GIL, not contention inside the kernel. The pool releases it
for the whole read.

The calling thread takes a share of the work, so `threads=4` means five
readers. `threads` is capped at `len(addrs) - 1` for the same reason.

Prefer grouping for dense addresses: one read per window beats one read per
address by more than threads can recover. Reach for threads when the
addresses are too spread out for grouping to apply.

## Building from source

```bash
python setup.py build_ext --inplace   # optional
python -m tests.test_fastmem          # functional tests
python -m tests.benchmarks            # benchmarks, own process
python -m tests.benchmarks --foreign  # benchmarks, foreign process
```

Building the extension needs MSVC and the Windows SDK. The SDK is not
always in the default location, so `setup.py` looks through `WindowsSdkDir`
and the usual paths including `G:\Windows Kits\10`. Some Python builds ship
no `pythonXY.lib` (built without `Py_ENABLE_SHARED`); in that case the
import library is generated from the DLL export table with `dumpbin` and
`lib.exe`.

## License

MIT. See [LICENSE](LICENSE).
