# fastmem for C and C++

The same engine as the Python package, without Python. Two artefacts:

| Path | What it is |
|---|---|
| `csrc/fastmem.h`, `csrc/fastmem.c` | plain C99, single translation unit, no dependencies beyond kernel32 |
| `cpp/include/fastmem/fastmem.hpp` | header-only C++17 wrapper: RAII, `std::vector`, exceptions |

The algorithms are identical to the Python extension, so both report the
same numbers: grouping, the persistent worker pool, and `read_region` as the
scanner primitive.

## Building

```bat
python tools\build_native.py                  :: fastmem.dll + fastmem.lib
python tools\build_native.py --example        :: also builds quickstart.exe
```

The script drives `cl.exe` with the include and library paths discovered
by `setup.py`, because this machine has no `vcvars` batch file. On a normal
developer machine the ordinary route works too:

```bat
cl /O2 /LD csrc\fastmem.c csrc\fastmem.def /Fe:fastmem.dll /link /OUT:fastmem.lib
```

`setup.py` reads `FASTMEM_BUILD_ARCH` (`x64`, `x86`, `arm64`) to pick the
MSVC and SDK library directories, which is how one x64 runner cross-compiles
all three targets.

## C

```c
#include "fastmem.h"

fastmem_proc *p = fastmem_open(1234);
if (!p) {
    fprintf(stderr, "%s\n", fastmem_strerror(fastmem_last_error()));
    return 1;
}

unsigned char buf[8];
if (fastmem_read(p, address, buf, sizeof buf))
    printf("hp = %p\n", *(void **)buf);

fastmem_close(p);
```

Batch reads write into one buffer and report which addresses worked:

```c
uintptr_t addrs[3] = { 0x1000, 0x2000, 0x3000 };
unsigned char out[24];
unsigned char mask[3];

size_t ok = fastmem_read_many(p, addrs, 3, 8, out, mask);
/* mask[i] is 1 when addrs[i] read, 0 when it did not */
```

Grouping turns dense input into a handful of calls instead of one per
address:

```c
fastmem_read_grouped(p, addrs, count, 8, 16 * 1024, out, mask);
```

Threading uses the shared worker pool, so there is nothing to create or
destroy around the call:

```c
fastmem_pool_read_many(p, addrs, count, 8, out, mask, 4);
```

## C++

```cpp
#include <fastmem/fastmem.hpp>

fastmem::Process p(1234);              // throws fastmem::error on failure

auto hp   = p.read<uintptr_t>(addr);   // typed, size taken from the type
auto blob = p.read_region(base, size); // one call, not one per page

std::vector<uint8_t> mask;
auto raw = p.read_grouped(addrs, 8, 16 * 1024, mask);

for (auto &region : p.readable_regions()) { /* ... */ }
```

Move-only, so there is no double-close. `find_all` sweeps the readable
regions rather than a raw address range:

```cpp
auto hits = p.find_all(&needle, sizeof needle, /*max_results=*/32);
```

## One trap worth knowing about

`PAGE_READWRITE` is `0x04` and does **not** carry `PAGE_READONLY`'s `0x02`
bit. The `PAGE_*` constants are an enumeration, not a permission mask, so
filtering regions with a plain AND against the raw value silently rejects
every writable region - which is most of a process, including the stack and
the heap.

fastmem therefore reports `protect` as `FM_PROTECT_*` permission bits and
exposes `fastmem_protect_flags()` to convert a raw `PAGE_*` value. This was
a real bug found by the example: the region count went from 44 to 86 when
it was fixed.

## Testing

`quickstart.exe` reads its own process, which needs no privileges, and
doubles as the smoke test:

```
=== search ===
marker at 0000007595AFFBA8 lives in region 0000007595AFF000..0000007595B00000 (4 KiB)
  search that region -> 2 hit(s) 0000007595AFFB68 0000007595AFFBA8  (marker located)
  readable_regions -> 86 regions, 1 cover the marker
  find_all -> 3 hit(s)

=== batched reads, 2000 addresses one page apart ===
read_many, 1 thread                    936.6 us/call   0.468 us/address
read_many_pooled, 4 workers             743.2 us/call   0.372 us/address
readable addresses: 2000 of 2000

=== grouping, 2000 addresses 64 bytes apart ===
read_many, 1 thread                    898.2 us/call   0.449 us/address
read_grouped, 16 KiB span                54.9 us/call   0.027 us/address
grouping speedup: 16.4x
```

Read your own process for these numbers, not a foreign one: self reads skip
the kernel transition, so the absolute values are optimistic and the ratios
are what to compare.