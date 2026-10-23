# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Correctness release. No API changes except the two documented below; every
item here was reachable from the public surface.

### Fixed

- `read_many(..., span=N)` with `N` smaller than `size` read past the end of
  the C scratch buffer and returned neighbouring heap instead of process
  memory: the grouping window was clamped to `span` while the copy still
  took `size` bytes. `p.read_many(addrs, 8, span=4)` was enough to hit it.
  The window now widens to at least one value. The standalone C library
  (`csrc/fastmem.c`) already clamped this; the extension did not.
- `find(..., align=N, chunk_size=M)` with `N > 1` returned nothing.
  Alignment was applied to the offset inside the window, but a chunked scan
  starts its window at a carried tail, so every hit was shifted by the
  window's own misalignment.
- `find(..., align=3)` and any other non-power-of-two alignment reported the
  wrong addresses: the mask shortcut only holds for powers of two.
- `find(..., chunk_size=M)` could report matches that do not exist after an
  unreadable block: the carried tail from before the hole was prepended to
  the next block, joining two ranges that were never adjacent.
- The pure-Python batch path handed `ReadProcessMemory` the address of a
  temporary buffer that had already been freed. Every `read_many` with
  `size > SCRATCH_SIZE` on the `python-ctypes` backend wrote into reclaimed
  heap.
- `backend.batch_into` on the pure-Python path raised `NameError` instead of
  reading: the call passed a variable that did not exist.
- `regions()` and `query()` shared one module-global
  `MEMORY_BASIC_INFORMATION`, so two threads walking different processes
  could read each other's region data. It is now per-thread.
- `one()` accepted a buffer shorter than `size`; `ReadProcessMemory` then
  wrote past its end. `backend.single` is public, so this was reachable.
- Buffer objects were not released on several error paths in the extension,
  leaving buffers permanently exported.
- ARM64 wheels for Python 3.13 were built without the extension: the x64
  `python313.lib` of the build interpreter was put ahead of the ARM64 import
  library on the link line, so every CPython symbol was unresolved. Cross
  builds no longer add the host's import library.

### Changed

- `read_many(..., as_=..., into=True)` now raises `ValueError`. It used to
  return the packed bytearray and silently drop `as_`.
- The extension reports the version from `pyproject.toml` instead of its own
  hardcoded string, so it cannot drift from the package. Same for
  `fastmem_version()` in the C library.
- SDK discovery scans drive letters instead of listing one machine's path.

## [0.1.0]

First public release.

### Added

**Reading**
- `read(addr, size=None, as_=None, or_none=False)` - the single read entry
  point. `size=None` uses the target pointer size, so `read(addr)` reads a
  pointer directly. `as_` interprets the bytes as a number
  (`'ptr'`, `'int'`, `'u32'`, `'f64'`, ...). `or_none=True` returns None
  instead of raising.
- `read_many(addrs, size=None, as_=None, into=False, span=-1, threads=0)` -
  batch reads. `into=True` returns one bytearray with no per-address
  objects. `span` groups nearby addresses into windows read with a single
  call. `threads` spreads work across threads.
- `read_region(base, size, chunks=0, or_none=False)` - contiguous read;
  `chunks=N` yields a generator of blocks instead.
- `find(pattern, regions=None, align=1, limit=0, chunk_size=0)` - search
  through `bytes.find` (C code), with alignment, result limit and
  overlapping block reads.

**Virtual memory**
- `regions(min_size, max_size, readable_only, committed_only, start, end)` -
  walk process memory through `VirtualQueryEx`.
- `query(addr)` - describe the region containing an address.
- `Region` - immutable region description: `base`, `size`, `state`,
  `protect`, `type`, `end`, `committed`, `readable`, `is_image`,
  `is_private`, `contains()`.

**Standalone C and C++**
- `csrc/fastmem.h`, `csrc/fastmem.c` - the same engine in plain C99, one
  translation unit, no dependency beyond kernel32. `fastmem_open`,
  `fastmem_read`, `fastmem_read_many`, `fastmem_read_grouped`,
  `fastmem_read_region`, `fastmem_find`, `fastmem_regions`, `fastmem_query`
  and the pool variants.
- `cpp/include/fastmem/fastmem.hpp` - header-only C++17 wrapper: move-only
  RAII handle, typed reads, `std::vector` containers, `fastmem::error`.
- `tools/build_native.py` builds both. Reads the target architecture from
  `FASTMEM_BUILD_ARCH`, which is what lets one x64 runner cross-compile
  x86 and ARM64.
- `docs/C_API.md` documents both and records the PAGE_* trap below.

**C extension** (`src/fastmem/_fastmem.c`, optional)
- `batch_release` - batched read with the GIL released.
- `batch_pooled`, `batch_pooled_bytes` - the same read spread across a pool
  of persistent worker threads. Workers are created on first use and park on
  an event between calls; building them per call cost ~812 us on Windows,
  more than the read itself. Measured 2.57x at 4 workers against 1.11x
  before, and 1.80x instead of 0.20x on addresses too sparse to group.
- `pool_workers()`, `pool_shutdown()` - inspect and drain the pool. Shutdown
  is registered with `Py_AtExit`, so no threads outlive the interpreter.
- `last_error()` - the Windows error code from the last failed read.
  `ctypes.get_last_error()` cannot see inside the extension, so without this
  every `ReadMemoryError` from a C-backed read reported code 0 and the
  "partial copy" hint never appeared.
- `batch_bytes`, `batch_grouped_bytes` - return ready lists of bytes, with
  no Python-side slicing.
- `batch_grouped` - grouped read writing into a caller buffer.
- `one`, `one_or`, `is_alive`.
- `backend.py` selects the implementation and falls back to pure Python
  silently; `Process.backend()` and `backend.HAVE_C` report the active one.
- `setup.py` builds it when possible. Missing compiler or SDK is not fatal.

**Other**
- Wheels for Python 3.9-3.13 on x64, x86 and ARM64 are built on GitHub by
  cibuildwheel and published to PyPI, so installing normally requires no
  compiler at all. A CI step fails the build if a wheel ships without the
  compiled extension, since that regression is silent otherwise.
- `read_many` accepts any iterable, including generators.
- Target bitness detection via `IsWow64Process2` → `IsWow64Process` →
  system architecture; exposes `pointer_size`, `is_64bit`, `machine`,
  `machine_name`, `page_size`.
- `is_alive()` through `GetExitCodeProcess`.
- Lazily formatted exception messages: building an error object costs
  ~0.4 us instead of ~3 us for a formatted string.

### Compatibility

- `as_='ptr'` follows the target bitness rather than always reading 8
  bytes. Previously WOW64 targets hit unmapped memory.
- Minimum access rights by default:
  `PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION` instead of
  `PROCESS_QUERY_INFORMATION`, which works without elevation for processes
  owned by the same user.
- Windows 7 through Server 2025; x86, x64, ARM64; Python 3.9-3.13; PyPy on
  pure Python.
- Only public Windows APIs. No hardcoded structure offsets, no assembly.
  Every optional API is feature-detected.
- Page size read from `GetSystemInfo`, never assumed.
- Protected processes (PPL) and PID 4 yield `ProcessOpenError` with
  readable text instead of crashing.
- A process dying mid-read never raises from `or_none=True`, and plain
  `read` raises `ProcessTerminatedError`.
- The C library reports region protection as FM_PROTECT_* permission bits
  rather than the raw PAGE_* value. PAGE_READWRITE (0x04) does not carry
  PAGE_READONLY's bit (0x02), so filtering on the raw value with a plain AND
  rejected every writable region - most of a process, including the stack
  and heap. The example caught it: the readable region count went from 44 to
  86 once fixed.

### Performance

Measured on a foreign process, microseconds per address, 8 bytes each:

| Operation | Python | C |
|---|---|---|
| batch into bytearray | 5.08 | 1.40 |
| batch into list[bytes] | 5.82 | 1.36 |
| batch into list[int] | 5.29 | 1.47 |
| grouping vs streaming | 5.12 | 0.076 |

The wins come from block size rather than language: one
`read_region(64 KiB)` costs ~22 us against ~150 ms for the same bytes read
address by address.

Threading is documented as 2.57x at 4 workers, not the 1.11x it measured
when the threads were built per call. Grouping still wins for dense
addresses; threads are for addresses too spread out to merge.

[0.1.0]: https://github.com/slitter-tech/fastmem/releases/tag/v0.1.0
