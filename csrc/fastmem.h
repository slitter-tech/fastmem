/*
 * fastmem.h - fast process memory reading for Windows, in plain C.
 *
 * The standalone counterpart of the Python package: same algorithms, no
 * Python, no dependencies beyond kernel32. Useful from C, and from C++
 * through the header-only wrapper in cpp/include/fastmem/fastmem.hpp.
 *
 * Design notes
 * ------------
 * Only public Windows APIs. No hardcoded structure offsets, no assembly,
 * nothing that has to be re-derived when Windows changes. Every optional
 * API is feature-detected at runtime.
 *
 * Errors are reported two ways on purpose: the calls return a plain int or
 * a bool so hot loops stay cheap, and fm_last_error() gives the Windows
 * code when something fails. Build with FM_STRICT_ERRORS to have failed
 * reads also call the error callback.
 *
 * Threading
 * ---------
 * fm_pool_* uses a pool of worker threads created on first use and reused
 * afterwards. Building threads per call costs more than the read itself on
 * Windows, which is why earlier revisions of this library documented
 * threading as a net loss.
 *
 * Example
 * -------
 *     fastmem_proc *p = fastmem_open(1234);
 *     if (!p) { fprintf(stderr, "%s\n", fastmem_strerror()); return 1; }
 *
 *     unsigned char buf[8];
 *     if (fastmem_read(p, address, buf, sizeof buf))
 *         printf("hp = %p\n", *(void **)buf);
 *
 *     fastmem_close(p);
 */

#ifndef FASTMEM_H
#define FASTMEM_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  define FM_API __declspec(dllexport)
#  if defined(FASTMEM_STATIC)
#    undef FM_API
#    define FM_API
#  endif
#else
#  error "fastmem targets Windows"
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* Types                                                               */
/* ------------------------------------------------------------------ */

/* Opaque process handle. Invalidates on fastmem_close. */
typedef struct fastmem_proc fastmem_proc;

/* Access mask for fastmem_open_ex. Same values as the Windows constants. */
#define FM_VM_READ         0x0010      /* PROCESS_VM_READ */
#define FM_QUERY_LIMITED   0x1000      /* PROCESS_QUERY_LIMITED_INFORMATION */
#define FM_VM_WRITE        0x0020      /* PROCESS_VM_WRITE */
#define FM_VM_OPERATION    0x0008      /* PROCESS_VM_OPERATION */
#define FM_ALL_ACCESS      0x1FFFFF1F  /* PROCESS_ALL_ACCESS */

/*
 * Region flags.
 *
 * FM_STATE_* and FM_TYPE_* are the Windows MEM_* bits and filter with a
 * plain AND.
 *
 * FM_PROTECT_* is NOT the Windows PAGE_* value, and that is deliberate.
 * PAGE_* is an enumeration, not a permission mask: PAGE_READWRITE is 0x04
 * and does not have PAGE_READONLY's 0x02 bit set, so
 *
 *     protect & FM_PROTECT_READ
 *
 * with FM_PROTECT_READ aliased to PAGE_READONLY rejects every writable
 * region, which is most of a process including the stack and the heap.
 * The values below are real permission bits, and
 * fm_protect_flags() converts a raw PAGE_* value into them.
 */
#define FM_STATE_COMMIT     0x1000
#define FM_STATE_RESERVE    0x2000
#define FM_STATE_FREE       0x10000

#define FM_PROTECT_NONE     0x01
#define FM_PROTECT_READ     0x02
#define FM_PROTECT_WRITE    0x04
#define FM_PROTECT_EXECUTE  0x08

#define FM_TYPE_PRIVATE     0x20000
#define FM_TYPE_MAPPED      0x40000
#define FM_TYPE_IMAGE       0x1000000

/* Raw Windows PAGE_* values, for fm_protect_flags(). */
#define FM_PAGE_NOACCESS           0x01
#define FM_PAGE_READONLY           0x02
#define FM_PAGE_READWRITE          0x04
#define FM_PAGE_WRITECOPY          0x08
#define FM_PAGE_EXECUTE            0x10
#define FM_PAGE_EXECUTE_READ       0x20
#define FM_PAGE_EXECUTE_READWRITE  0x40
#define FM_PAGE_EXECUTE_WRITECOPY  0x80
#define FM_PAGE_GUARD              0x100
#define FM_PAGE_NOCACHE            0x200
#define FM_PAGE_WRITECOMBINE       0x400

/* A described region of process virtual memory. */
typedef struct {
    uintptr_t base;      /* start address                        */
    size_t    size;      /* length in bytes                      */
    unsigned  state;     /* FM_STATE_*                           */
    unsigned  protect;   /* FM_PROTECT_* permission bits         */
    unsigned  type;      /* FM_TYPE_*                            */
} fastmem_region;

/* Description of one page, as returned by fastmem_query. */
typedef struct {
    fastmem_region region;
    uintptr_t allocation_base;   /* start of the owning allocation */
} fastmem_page;

/* Called for every region matching a filter. Return 0 to stop walking. */
typedef int (*fastmem_region_cb)(const fastmem_region *region, void *user);

/* Called when a read fails. Optional; see FM_STRICT_ERRORS. */
typedef void (*fastmem_error_cb)(uintptr_t address, size_t size,
                                 unsigned long code, void *user);

/* ------------------------------------------------------------------ */
/* Process lifecycle                                                   */
/* ------------------------------------------------------------------ */

/*
 * Open by PID with the minimum rights fastmem needs:
 * PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION. That combination
 * works without elevation for processes owned by the current user, unlike
 * PROCESS_QUERY_INFORMATION.
 */
FM_API fastmem_proc *fastmem_open(unsigned pid);

/* Open with an explicit access mask, e.g. FM_VM_READ | FM_ALL_ACCESS. */
FM_API fastmem_proc *fastmem_open_ex(unsigned pid, unsigned access);

/* Release the handle. Safe on NULL. The pointer is invalid afterwards. */
FM_API void fastmem_close(fastmem_proc *proc);

/* PID of an open process, or 0. */
FM_API unsigned fastmem_pid(const fastmem_proc *proc);

/* TRUE while the process is running. Also FALSE for a closed handle. */
FM_API int fastmem_is_alive(const fastmem_proc *proc);

/* ------------------------------------------------------------------ */
/* Reads                                                               */
/* ------------------------------------------------------------------ */

/*
 * Read `size` bytes at `addr` into `buf`.
 * Returns 1 on success, 0 on failure with the code in fm_last_error().
 * A partial copy counts as a failure: silently returning short data from a
 * racing target would be worse than an error.
 */
FM_API int fastmem_read(fastmem_proc *proc, uintptr_t addr,
                        void *buf, size_t size);

/*
 * Read `count` addresses of `size` bytes each into `out`, which must hold
 * count * size bytes. `mask`, when not NULL, receives one byte per address:
 * 1 on success, 0 on failure.
 * Returns the number of successful reads.
 */
FM_API size_t fastmem_read_many(fastmem_proc *proc, const uintptr_t *addrs,
                                size_t count, size_t size,
                                void *out, unsigned char *mask);

/*
 * Same, but addresses closer than `span` bytes are merged into windows read
 * with a single call. This is the single largest win in the library: for
 * dense input it replaces one call per address with one per window.
 *
 * Addresses are sorted internally, so order does not matter and the caller's
 * `mask` is written in the caller's original order. Pass span = 0 to
 * disable grouping.
 */
FM_API size_t fastmem_read_grouped(fastmem_proc *proc, const uintptr_t *addrs,
                                   size_t count, size_t size, size_t span,
                                   void *out, unsigned char *mask);

/*
 * Read one contiguous range. This is the primitive for scanners: walking
 * every mapped region costs one call per region instead of thousands per
 * page. Returns 1 on success.
 */
FM_API int fastmem_read_region(fastmem_proc *proc, uintptr_t base,
                               void *out, size_t size);

/*
 * Search `size` bytes at `base` for `needle`. Writes matching addresses to
 * `results` up to `max_results` and returns how many were found, which can
 * exceed max_results only to report that the cap was hit.
 *
 * `align` > 1 restricts matches to addresses divisible by `align`, which is
 * what you want when scanning for pointers or a structure field.
 */
FM_API size_t fastmem_find(fastmem_proc *proc, uintptr_t base, size_t size,
                           const void *needle, size_t needle_len,
                           uintptr_t *results, size_t max_results,
                           size_t align);

/* ------------------------------------------------------------------ */
/* Regions                                                             */
/* ------------------------------------------------------------------ */

/*
 * Walk the address space, calling `cb` for every region matching the flag
 * filter. Pass 0 for any of the three flags to accept anything.
 * Returns the number of regions passed to the callback.
 */
FM_API size_t fastmem_regions(fastmem_proc *proc, unsigned state,
                              unsigned protect, unsigned type,
                              size_t min_size, size_t max_size,
                              fastmem_region_cb cb, void *user);

/* Describe the region containing `addr`. Returns 1 on success. */
FM_API int fastmem_query(fastmem_proc *proc, uintptr_t addr,
                         fastmem_page *out);

/* System page size, from GetSystemInfo rather than assumed. */
FM_API size_t fastmem_page_size(void);

/*
 * Convert a raw Windows PAGE_* value into FM_PROTECT_* permission bits.
 *
 * Needed because PAGE_READWRITE does not carry PAGE_READONLY's bit, so the
 * raw values cannot be tested with a bitwise AND. PAGE_GUARD, PAGE_NOCACHE
 * and PAGE_WRITECOMBINE are modifier bits and are stripped.
 */
FM_API unsigned fastmem_protect_flags(unsigned page_protect);

/* ------------------------------------------------------------------ */
/* Threading                                                           */
/* ------------------------------------------------------------------ */

/*
 * fastmem_read_many across `workers` helper threads. The calling thread
 * takes a share too, so workers = 1 is already two-way parallelism. The
 * worker pool is created on first use and reused.
 */
FM_API size_t fastmem_pool_read_many(fastmem_proc *proc,
                                     const uintptr_t *addrs,
                                     size_t count, size_t size,
                                     void *out, unsigned char *mask,
                                     unsigned workers);

/* Number of live worker threads; 0 before the pool is used. */
FM_API unsigned fastmem_pool_workers(void);

/* Stop all worker threads. Optional: also runs automatically at exit. */
FM_API void fastmem_pool_shutdown(void);

/* ------------------------------------------------------------------ */
/* Diagnostics                                                         */
/* ------------------------------------------------------------------ */

/* Windows error code from the last failed call, 0 if none. */
FM_API unsigned long fastmem_last_error(void);

/* Human readable text for a code, and for the last error. */
FM_API const char *fastmem_strerror(unsigned long code);

/* Library version, e.g. "0.1.0". */
FM_API const char *fastmem_version(void);

/*
 * Error codes the library reports for conditions Windows has no code for.
 * Kept above 0xFFFF so they cannot collide with real Win32 codes.
 */
#define FM_ERR_CLOSED      0x10001u   /* handle already released      */
#define FM_ERR_INVALID     0x10002u   /* bad argument                */
#define FM_ERR_NO_MEMORY   0x10003u   /* allocation failed            */
#define FM_ERR_OVERFLOW    0x10004u   /* size computation overflowed  */

/* Default page bytes per memoryview, matching the Python package. */
#define FM_DEFAULT_SPAN 16384

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* FASTMEM_H */