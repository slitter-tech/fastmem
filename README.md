[Русский](README_RU.md) | **English**

# fastmem

[![PyPI](https://img.shields.io/pypi/v/fastmem.svg)](https://pypi.org/project/fastmem/)
[![Python](https://img.shields.io/pypi/pyversions/fastmem.svg)](https://pypi.org/project/fastmem/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

High-performance process memory reading for Windows. Pure `ctypes` and the
standard library. An optional C extension is built automatically when a
compiler is available, but is never required.

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
elements, list nodes - and grouping is up to **22x** faster than reading
them one at a time.

`threads` spreads the work over several threads and requires the C
extension. Measured gain is modest (1.2-1.4x) because thread creation is
paid on every call; grouping wins whenever it applies. Use `span=0` with
`threads` when the addresses are too spread out for grouping.

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
exception: it manages its own threads and is safe to call.

## Performance

Python 3.11, 4 cores, foreign process, 8 bytes per address. Baseline before
any optimisation was 9.3 us per call.

| Operation | Python | C | Speedup |
|---|---|---|---|
| `read()` in try/except, bad address | 12.80 | - | - |
| `read(or_none=True)`, bad address | 6.13 | - | - |
| `read()` success | 7.33 | - | - |
| batch into `bytearray` | 6.04 | 2.14 | 2.82x |
| batch into `list[bytes]` | 7.88 | 1.83 | 4.32x |
| batch into `list[int]` | 6.86 | 1.99 | 3.45x |
| **grouping vs streaming** | 6.51 | **0.30** | **22.0x** |

Large blocks, one API call each:

| | us | MB/s |
|---|---|---|
| `read_region(4 KiB)` | 9.3 | 442 |
| `read_region(64 KiB)` | 30.9 | 2120 |
| `read_region(1 MiB)` | 1080 | 971 |

### Where the speed comes from

1. **Block size matters more than language.** One `read_region(64 KiB)`
   costs ~31 us; reading the same 64 KiB address by address costs ~150 ms.
2. **Search belongs in C.** `bytes.find` beats a Python loop by five orders
   of magnitude.
3. **Allocation zeroes memory.** `(ctypes.c_char * n)()` memsets: ~400 us
   for 1 MiB. A reusable buffer removes it.
4. **Exceptions are expensive.** Formatting the error text cost 3.3 us; the
   message is now built lazily in `__str__`.
5. **C means one boundary crossing instead of a thousand.** A Python → C
   call costs ~0.6 us, and a 1000-address batch in Python pays that a
   thousand times. The C loop crosses once.

### Threads

Measured on a foreign process, no grouping:

| | 1 thread | 2 | 4 |
|---|---|---|---|
| C, GIL released | 1.97 | 1.44 (1.37x) | 1.66 (1.19x) |

Modest, because thread creation is paid on every call. Without the
extension threads are actively harmful. A control group confirms the
mechanism: a variant that holds the GIL gains exactly nothing from four
threads (1617 us against 1605 us for one), so the limiter is the GIL, not
contention inside the kernel.

Prefer grouping. Reach for threads only when the addresses are too spread
out for grouping to apply.

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
