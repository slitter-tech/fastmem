# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

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

**C extension** (`src/fastmem/_fastmem.c`, optional)
- `batch_release` - batched read with the GIL released.
- `batch_bytes`, `batch_grouped_bytes` - return ready lists of bytes, with
  no Python-side slicing.
- `batch_grouped` - grouped read writing into a caller buffer.
- `one`, `is_alive`.
- `backend.py` selects the implementation and falls back to pure Python
  silently; `Process.backend()` and `backend.HAVE_C` report the active one.
- `setup.py` builds it when possible. Missing compiler or SDK is not fatal.

**Other**
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

### Performance

Measured on a foreign process, microseconds per address, 8 bytes each:

| Operation | Python | C |
|---|---|---|
| batch into bytearray | 6.04 | 2.14 |
| batch into list[bytes] | 7.88 | 1.83 |
| batch into list[int] | 6.86 | 1.99 |
| grouping vs streaming | 6.51 | 0.30 |

The wins come from block size rather than language: one
`read_region(64 KiB)` costs ~31 us against ~150 ms for the same bytes read
address by address. Threading is deliberately documented as a modest 1.2-1.4x
that loses to grouping, since thread creation is paid on every call.

[0.1.0]: https://github.com/slitter-tech/fastmem/releases/tag/v0.1.0
