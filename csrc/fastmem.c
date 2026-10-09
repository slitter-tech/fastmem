/*
 * fastmem.c - implementation. See fastmem.h for the API.
 *
 * The algorithms match the Python extension one for one, so both report
 * the same numbers. The grouping and pool code in particular is the same
 * design: sort, merge into windows, one ReadProcessMemory per window; and
 * a persistent worker pool rather than threads per call.
 */

/* Not defined here on purpose: the build system may already pass them, and
 * redefining produces warning C4005, which is an error under /WX. */
#include "fastmem.h"

#include <windows.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

struct fastmem_proc {
    HANDLE handle;
    unsigned pid;
};

/*
 * GetLastError of the last failed call.
 *
 * A plain static rather than per-thread: it is read only right after a
 * failure and it holds a reason code, not state. Threads that call
 * concurrently overwrite each other here, which is documented - callers
 * that need a reliable code must be the only one in flight.
 */
static DWORD g_last_error = 0;

static unsigned long g_sticky_error = 0;   /* FM_ERR_* values */

static void set_win_error(void)
{
    g_last_error = GetLastError();
}

static void set_lib_error(unsigned long code)
{
    g_last_error = 0;
    g_sticky_error = code;
}

static void clear_error(void)
{
    g_last_error = 0;
    g_sticky_error = 0;
}

unsigned long fastmem_last_error(void)
{
    if (g_sticky_error)
        return g_sticky_error;
    return (unsigned long)g_last_error;
}

/* ------------------------------------------------------------------ */
/* String helpers                                                      */
/* ------------------------------------------------------------------ */

const char *fastmem_strerror(unsigned long code)
{
    switch (code) {
    case 0:
        return "no error";
    case FM_ERR_CLOSED:
        return "process already closed";
    case FM_ERR_INVALID:
        return "invalid argument";
    case FM_ERR_NO_MEMORY:
        return "out of memory";
    case FM_ERR_OVERFLOW:
        return "size computation overflowed";
    case ERROR_ACCESS_DENIED:
        return "access denied - administrator rights are required, or the "
               "process is protected (PPL / anti-cheat)";
    case ERROR_INVALID_HANDLE:
        return "invalid handle - the process has exited or was killed";
    case ERROR_INVALID_PARAMETER:
        return "invalid parameter - the address is outside the process "
               "address space, or the size is malformed";
    case ERROR_PARTIAL_COPY:
        return "partial copy - the range crosses into unmapped memory";
    case ERROR_NOT_FOUND:
        return "not found";
    default:
        return "unknown error";
    }
}

const char *fastmem_version(void)
{
    return "0.1.0";
}

size_t fastmem_page_size(void)
{
    static size_t cached = 0;
    if (!cached) {
        SYSTEM_INFO si;
        GetSystemInfo(&si);
        cached = si.dwPageSize ? si.dwPageSize : 4096;
    }
    return cached;
}

unsigned fastmem_protect_flags(unsigned page_protect)
{
    /* The low byte is the protection kind; the rest are modifiers that
     * change how it is honoured, not what may be done to it. */
    unsigned kind = page_protect & 0xFFu;

    switch (kind) {
    case PAGE_NOACCESS:
        return FM_PROTECT_NONE;
    case PAGE_READONLY:
        return FM_PROTECT_READ;
    case PAGE_READWRITE:
    case PAGE_WRITECOPY:
        return FM_PROTECT_READ | FM_PROTECT_WRITE;
    case PAGE_EXECUTE:
        return FM_PROTECT_EXECUTE;
    case PAGE_EXECUTE_READ:
        return FM_PROTECT_EXECUTE | FM_PROTECT_READ;
    case PAGE_EXECUTE_READWRITE:
    case PAGE_EXECUTE_WRITECOPY:
        return FM_PROTECT_EXECUTE | FM_PROTECT_READ | FM_PROTECT_WRITE;
    default:
        return FM_PROTECT_NONE;
    }
}

/* ------------------------------------------------------------------ */
/* Process lifecycle                                                   */
/* ------------------------------------------------------------------ */

static fastmem_proc *proc_from_handle(HANDLE handle, unsigned pid)
{
    fastmem_proc *proc = (fastmem_proc *)malloc(sizeof *proc);
    if (!proc) {
        CloseHandle(handle);
        set_lib_error(FM_ERR_NO_MEMORY);
        return NULL;
    }
    proc->handle = handle;
    proc->pid = pid;
    return proc;
}

fastmem_proc *fastmem_open_ex(unsigned pid, unsigned access)
{
    HANDLE h;

    clear_error();
    if (!pid)
        return NULL;

    h = OpenProcess((DWORD)access, FALSE, (DWORD)pid);
    if (!h) {
        set_win_error();
        return NULL;
    }
    return proc_from_handle(h, pid);
}

fastmem_proc *fastmem_open(unsigned pid)
{
    /* PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION: the latter is
     * all that is needed for GetExitCodeProcess and works without
     * elevation for same-user processes, unlike PROCESS_QUERY_INFORMATION. */
    return fastmem_open_ex(pid, FM_VM_READ | FM_QUERY_LIMITED);
}

void fastmem_close(fastmem_proc *proc)
{
    if (!proc)
        return;
    if (proc->handle)
        CloseHandle(proc->handle);
    proc->handle = NULL;
    free(proc);
}

unsigned fastmem_pid(const fastmem_proc *proc)
{
    return proc ? proc->pid : 0;
}

int fastmem_is_alive(const fastmem_proc *proc)
{
    DWORD code = 0;

    if (!proc || !proc->handle)
        return 0;
    if (!GetExitCodeProcess(proc->handle, &code)) {
        set_win_error();
        return 0;
    }
    return code == STILL_ACTIVE;
}

/* ------------------------------------------------------------------ */
/* Single reads                                                        */
/* ------------------------------------------------------------------ */

int fastmem_read(fastmem_proc *proc, uintptr_t addr, void *buf, size_t size)
{
    SIZE_T got = 0;

    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (!buf) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }
    if (size == 0)
        return 1;

    if (!ReadProcessMemory(proc->handle, (LPCVOID)addr, buf,
                           (SIZE_T)size, &got)
        || got != (SIZE_T)size) {
        set_win_error();
        return 0;
    }
    return 1;
}

int fastmem_read_region(fastmem_proc *proc, uintptr_t base, void *out,
                        size_t size)
{
    return fastmem_read(proc, base, out, size);
}

/* ------------------------------------------------------------------ */
/* Batched reads                                                       */
/* ------------------------------------------------------------------ */

/*
 * One read per address. Kept separate from the wrappers so the same body
 * can run with the calling convention each caller wants.
 */
static size_t run_many(HANDLE handle, const uintptr_t *addrs, size_t count,
                       size_t size, unsigned char *out, unsigned char *mask)
{
    size_t i, ok = 0;
    SIZE_T got = 0;
    DWORD err = 0;

    for (i = 0; i < count; ++i) {
        unsigned char *dst = out + i * size;
        if (ReadProcessMemory(handle, (LPCVOID)addrs[i], dst,
                              (SIZE_T)size, &got)
            && got == (SIZE_T)size) {
            if (mask)
                mask[i] = 1;
            ++ok;
        } else {
            if (mask)
                mask[i] = 0;
            if (!err)
                err = GetLastError();
        }
    }
    g_last_error = err;
    g_sticky_error = 0;
    return ok;
}

size_t fastmem_read_many(fastmem_proc *proc, const uintptr_t *addrs,
                         size_t count, size_t size, void *out,
                         unsigned char *mask)
{
    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (count == 0)
        return 0;
    if (!addrs || !out || size == 0) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }
    /* count * size must not wrap, or the loop writes past `out`. */
    if (count > (size_t)-1 / size) {
        set_lib_error(FM_ERR_OVERFLOW);
        return 0;
    }
    return run_many(proc->handle, addrs, count, size,
                    (unsigned char *)out, mask);
}

/* ------------------------------------------------------------------ */
/* Grouping                                                            */
/* ------------------------------------------------------------------ */

/*
 * Index array sorted by address. An insertion sort is the right choice
 * here: addresses usually arrive nearly ordered, and qsort's callback per
 * comparison costs more than the reads it would order.
 */
static void sort_indices(const uintptr_t *addrs, size_t *order, size_t count)
{
    size_t i, j;

    for (i = 1; i < count; ++i) {
        size_t key = order[i];
        uintptr_t key_addr = addrs[key];
        j = i;
        while (j > 0 && addrs[order[j - 1]] > key_addr) {
            order[j] = order[j - 1];
            --j;
        }
        order[j] = key;
    }
}

/*
 * One read per window of nearby addresses.
 *
 * Sorting is by address, not index, so caller order is preserved in the
 * mask: every original position still gets its own result byte.
 */
static size_t run_grouped(HANDLE handle, const uintptr_t *addrs, size_t count,
                          size_t size, size_t span, unsigned char *out,
                          unsigned char *mask)
{
    size_t *order;
    unsigned char *scratch;
    size_t i, ok = 0;

    if (count > (size_t)-1 / size || span > (size_t)-1 / count + 1) {
        set_lib_error(FM_ERR_OVERFLOW);
        return 0;
    }

    order = (size_t *)malloc(count * sizeof *order);
    scratch = (unsigned char *)malloc(span ? span : 1);
    if (!order || !scratch) {
        free(order);
        free(scratch);
        set_lib_error(FM_ERR_NO_MEMORY);
        return 0;
    }

    for (i = 0; i < count; ++i)
        order[i] = i;
    sort_indices(addrs, order, count);

    i = 0;
    while (i < count) {
        uintptr_t base = addrs[order[i]];
        uintptr_t end = base + (uintptr_t)size;
        size_t width, j;
        SIZE_T got = 0;

        /* widen the window while the next address still fits */
        j = i;
        while (j + 1 < count) {
            uintptr_t next = addrs[order[j + 1]];
            if ((unsigned long long)(next + (uintptr_t)size - base) > span)
                break;
            ++j;
            end = next + (uintptr_t)size;
        }

        width = (size_t)(end - base);
        if (width > span)
            width = span;

        if (ReadProcessMemory(handle, (LPCVOID)base, scratch,
                              (SIZE_T)width, &got)
            && got == (SIZE_T)width) {
            size_t k;
            for (k = i; k <= j; ++k) {
                size_t idx = order[k];
                uintptr_t off = addrs[idx] - base;
                memcpy(out + idx * size, scratch + off, size);
                if (mask)
                    mask[idx] = 1;
                ++ok;
            }
        }
        i = j + 1;
    }

    free(scratch);
    free(order);
    g_last_error = 0;
    g_sticky_error = 0;
    return ok;
}

size_t fastmem_read_grouped(fastmem_proc *proc, const uintptr_t *addrs,
                            size_t count, size_t size, size_t span,
                            void *out, unsigned char *mask)
{
    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (count == 0)
        return 0;
    if (!addrs || !out || size == 0) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }
    if (span == 0)
        return run_many(proc->handle, addrs, count, size,
                        (unsigned char *)out, mask);
    if (span < size)
        span = size;
    return run_grouped(proc->handle, addrs, count, size, span,
                       (unsigned char *)out, mask);
}

/* ------------------------------------------------------------------ */
/* Search                                                              */
/* ------------------------------------------------------------------ */

/*
 * Byte search over a window of the target.
 *
 * memmem is not available on MSVC, so this is the plain scan. It is not
 * the bottleneck: fastmem_find spends microseconds searching a 64 KiB
 * window against hundreds of microseconds reading it from the target, so a
 * memcmp per candidate position is free by comparison. Correctness is
 * worth more here than the last few percent.
 */
static const unsigned char *mem_find(const unsigned char *hay, size_t hay_len,
                                     const unsigned char *needle,
                                     size_t needle_len)
{
    const unsigned char *p, *last;

    if (needle_len == 0)
        return hay;
    if (needle_len > hay_len)
        return NULL;

    /* memchr skips to the next candidate for the first byte. Only sound
     * while that byte cannot repeat inside the needle. */
    if (needle_len == 1 || needle[0] != needle[1]) {
        const unsigned char *end = hay + hay_len - needle_len;
        p = (const unsigned char *)memchr(hay, needle[0],
                                          (size_t)(end - hay) + 1);
        while (p && p <= end) {
            if (memcmp(p, needle, needle_len) == 0)
                return p;
            p = (const unsigned char *)memchr(p + 1, needle[0],
                                              (size_t)(end - p));
        }
        return NULL;
    }

    last = hay + hay_len - needle_len;
    for (p = hay; p <= last; ++p) {
        if (*p == needle[0] && memcmp(p, needle, needle_len) == 0)
            return p;
    }
    return NULL;
}

size_t fastmem_find(fastmem_proc *proc, uintptr_t base, size_t size,
                    const void *needle, size_t needle_len,
                    uintptr_t *results, size_t max_results, size_t align)
{
    unsigned char *buf = NULL, *win = NULL;
    size_t chunk, carry = 0, offset = 0, found = 0;
    int truncated = 0;

    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (!needle || needle_len == 0 || align == 0) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }

    /* Enough to hold a chunk plus the tail needed to catch a match
     * straddling two chunks. */
    chunk = 64 * 1024;
    if (chunk < needle_len)
        chunk = needle_len;
    if (chunk > size)
        chunk = size;

    buf = (unsigned char *)malloc(chunk);
    if (!buf) {
        set_lib_error(FM_ERR_NO_MEMORY);
        return 0;
    }

    while (offset < size) {
        size_t want = chunk - carry;
        size_t n, pos;
        uintptr_t win_base;

        if (want > size - offset)
            want = size - offset;

        if (!fastmem_read(proc, base + offset, buf + carry, want)) {
            /*
             * Skip the unreadable chunk and keep going. Stopping here would
             * mean a search over an address space finds nothing as soon as
             * it meets the first unmapped page, which is page zero for
             * every process - the common case, not the exceptional one.
             *
             * The carry must be dropped: the bytes at the front of the
             * buffer came from before the hole, and splicing them onto the
             * next readable chunk would build a window out of two
             * non-adjacent regions and report matches that do not exist in
             * either.
             */
            carry = 0;
            offset += want;
            continue;
        }
        n = carry + want;

        /* One extra bytes-object per chunk is cheaper than re-reading on
         * every match, and matches are rare. */
        if (!win) {
            win = (unsigned char *)malloc(n + 1);
            if (!win) {
                free(buf);
                set_lib_error(FM_ERR_NO_MEMORY);
                return 0;
            }
        }
        memcpy(win, buf, n);
        win_base = base + offset - carry;

        pos = 0;
        while (pos + needle_len <= n) {
            const unsigned char *hit = mem_find(win + pos, n - pos,
                                                (const unsigned char *)needle,
                                                needle_len);
            size_t at;
            if (!hit)
                break;
            at = (size_t)(hit - win);
            pos = at + 1;
            if (align > 1 && ((win_base + at) % align) != 0)
                continue;
            if (found < max_results)
                results[found] = win_base + at;
            ++found;
            if (found >= max_results) {
                truncated = 1;
                break;
            }
        }

        if (truncated)
            break;

        /* Carry the tail so a match spanning the boundary is still seen. */
        carry = n >= needle_len - 1 ? needle_len - 1 : n;
        if (carry)
            memmove(buf, win + n - carry, carry);
        offset += want;
        if (carry == 0 && want == 0)
            break;
    }

    free(win);
    free(buf);
    return found;
}

/* ------------------------------------------------------------------ */
/* Regions                                                             */
/* ------------------------------------------------------------------ */

typedef struct {
    fastmem_region_cb cb;
    void *user;
    size_t count;
    unsigned state, protect, type;
    size_t min_size, max_size;
} walk_ctx;

static int region_matches(const walk_ctx *w, const fastmem_region *r)
{
    if (w->state && !(r->state & w->state))
        return 0;
    if (w->protect && !(r->protect & w->protect))
        return 0;
    if (w->type && !(r->type & w->type))
        return 0;
    if (w->min_size && r->size < w->min_size)
        return 0;
    if (w->max_size && r->size > w->max_size)
        return 0;
    return 1;
}

static int walk_next(fastmem_proc *proc, MEMORY_BASIC_INFORMATION *mbi,
                     uintptr_t *cursor, walk_ctx *w)
{
    int stop = 0;

    while (*cursor < (uintptr_t)0x7FFFFFFF0000ULL) {
        if (VirtualQueryEx(proc->handle, (LPCVOID)*cursor, mbi,
                           sizeof *mbi) == 0)
            break;
        if (mbi->RegionSize == 0)
            break;

        *cursor += mbi->RegionSize;
        if (mbi->State == MEM_FREE) {
            /* A free region still advances the cursor; only allocation
             * bases matter for a fresh starting point. */
            continue;
        }

        {
            fastmem_region r;
            r.base = (uintptr_t)mbi->BaseAddress;
            r.size = (size_t)mbi->RegionSize;
            r.state = (unsigned)mbi->State;
            r.protect = fastmem_protect_flags((unsigned)mbi->Protect);
            r.type = (unsigned)mbi->Type;
            if (region_matches(w, &r)) {
                ++w->count;
                if (!w->cb(&r, w->user)) {
                    stop = 1;
                    break;
                }
            }
        }
    }
    return stop;
}

size_t fastmem_regions(fastmem_proc *proc, unsigned state, unsigned protect,
                       unsigned type, size_t min_size, size_t max_size,
                       fastmem_region_cb cb, void *user)
{
    MEMORY_BASIC_INFORMATION mbi;
    walk_ctx w;

    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (!cb) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }

    w.cb = cb;
    w.user = user;
    w.count = 0;
    w.state = state;
    w.protect = protect;
    w.type = type;
    w.min_size = min_size;
    w.max_size = max_size;

    memset(&mbi, 0, sizeof mbi);
    {
        uintptr_t cursor = 0;
        walk_next(proc, &mbi, &cursor, &w);
    }
    return w.count;
}

int fastmem_query(fastmem_proc *proc, uintptr_t addr, fastmem_page *out)
{
    MEMORY_BASIC_INFORMATION mbi;

    if (!proc || !proc->handle || !out) {
        set_lib_error(proc && !out ? FM_ERR_INVALID : FM_ERR_CLOSED);
        return 0;
    }
    if (VirtualQueryEx(proc->handle, (LPCVOID)addr, &mbi, sizeof mbi) == 0) {
        set_win_error();
        return 0;
    }
    out->region.base = (uintptr_t)mbi.BaseAddress;
    out->region.size = (size_t)mbi.RegionSize;
    out->region.state = (unsigned)mbi.State;
    out->region.protect = fastmem_protect_flags((unsigned)mbi.Protect);
    out->region.type = (unsigned)mbi.Type;
    out->allocation_base = (uintptr_t)mbi.AllocationBase;
    return 1;
}

/* ------------------------------------------------------------------ */
/* Worker pool                                                         */
/* ------------------------------------------------------------------ */

/*
 * Persistent workers, created on demand and parked between calls.
 *
 * Creating and joining threads per call costs more than the read on
 * Windows, which is what made threading a net loss in the first
 * measurement. The pool pays that once.
 */
#define FM_POOL_MAX 32

typedef struct {
    HANDLE thread;
    HANDLE wake;            /* manual reset: a job is waiting */
    HANDLE handle;
    const uintptr_t *addrs;
    size_t count, size, first, last;
    unsigned char *out;
    unsigned char *mask;
    LONG shutdown;
} fm_worker;

static fm_worker g_pool[FM_POOL_MAX];
static unsigned g_pool_size = 0;
static CRITICAL_SECTION g_pool_lock;
static LONG g_pool_ready = 0;
static HANDLE g_chunk_done = NULL;   /* counting semaphore */

static void ensure_pool(void)
{
    if (InterlockedCompareExchange(&g_pool_ready, 1, 0) == 0) {
        InitializeCriticalSection(&g_pool_lock);
        g_chunk_done = CreateSemaphore(NULL, 0, FM_POOL_MAX, NULL);
    }
}

static void run_range(fm_worker *w)
{
    size_t i;
    SIZE_T got = 0;
    DWORD err = 0;

    for (i = w->first; i < w->last; ++i) {
        unsigned char *dst = w->out + i * w->size;
        if (ReadProcessMemory(w->handle, (LPCVOID)w->addrs[i], dst,
                              (SIZE_T)w->size, &got)
            && got == (SIZE_T)w->size) {
            if (w->mask)
                w->mask[i] = 1;
        } else {
            if (w->mask)
                w->mask[i] = 0;
            if (!err)
                err = GetLastError();
        }
    }
    if (err && !g_last_error)
        g_last_error = err;
}

static DWORD WINAPI worker_main(LPVOID arg)
{
    fm_worker *w = (fm_worker *)arg;

    for (;;) {
        if (WaitForSingleObject(w->wake, INFINITE) != WAIT_OBJECT_0)
            break;
        /* Consume the signal before anything else. Leaving the event set
         * would run this job twice and desynchronise the dispatcher. */
        ResetEvent(w->wake);
        if (InterlockedCompareExchange(&w->shutdown, 0, 0))
            break;
        run_range(w);
        ReleaseSemaphore(g_chunk_done, 1, NULL);
    }
    return 0;
}

static void pool_assign(HANDLE handle, const uintptr_t *addrs, size_t count,
                        size_t size, unsigned char *out,
                        unsigned char *mask, unsigned workers,
                        size_t chunk)
{
    unsigned i;

    for (i = 1; i < workers; ++i) {
        fm_worker *w = &g_pool[i];
        size_t first = (size_t)i * chunk;
        size_t last = first + chunk;
        if (first > count)
            first = count;
        if (last > count)
            last = count;
        w->handle = handle;
        w->addrs = addrs;
        w->count = count;
        w->size = size;
        w->out = out;
        w->mask = mask;
        w->first = first;
        w->last = last;
        SetEvent(w->wake);
    }
}

size_t fastmem_pool_read_many(fastmem_proc *proc, const uintptr_t *addrs,
                              size_t count, size_t size, void *out,
                              unsigned char *mask, unsigned workers)
{
    size_t chunk, ok = 0, i;
    unsigned done = 0;
    unsigned char *bytes = (unsigned char *)out;

    if (!proc || !proc->handle) {
        set_lib_error(FM_ERR_CLOSED);
        return 0;
    }
    if (count == 0)
        return 0;
    if (!addrs || !out || size == 0) {
        set_lib_error(FM_ERR_INVALID);
        return 0;
    }
    if (count > (size_t)-1 / size) {
        set_lib_error(FM_ERR_OVERFLOW);
        return 0;
    }
    if (workers < 1)
        workers = 1;
    if ((size_t)workers > count - 1)
        workers = (unsigned)(count - 1);   /* the caller takes a chunk */
    if (workers < 1)
        return run_many(proc->handle, addrs, count, size, bytes, mask);

    ensure_pool();
    EnterCriticalSection(&g_pool_lock);

    while (g_pool_size < workers) {
        fm_worker *w = &g_pool[g_pool_size];
        w->wake = CreateEvent(NULL, TRUE, FALSE, NULL);
        if (!w->wake)
            break;
        w->shutdown = 0;
        w->thread = CreateThread(NULL, 0, worker_main, w, 0, NULL);
        if (!w->thread) {
            CloseHandle(w->wake);
            w->wake = NULL;
            break;
        }
        g_pool_size++;
    }
    if (g_pool_size < workers)
        workers = g_pool_size;
    if (workers < 1) {
        LeaveCriticalSection(&g_pool_lock);
        return run_many(proc->handle, addrs, count, size, bytes, mask);
    }

    chunk = (count + workers - 1) / workers;

    /* Should already be zero: every worker signals once per job and all are
     * counted below. Draining makes an early return impossible. */
    while (WaitForSingleObject(g_chunk_done, 0) == WAIT_OBJECT_0)
        ;

    pool_assign(proc->handle, addrs, count, size, bytes, mask,
                workers, chunk);

    /* The calling thread takes chunk 0 rather than idling. */
    {
        SIZE_T got = 0;
        for (i = 0; i < chunk && i < count; ++i) {
            unsigned char *dst = bytes + i * size;
            if (ReadProcessMemory(proc->handle, (LPCVOID)addrs[i], dst,
                                  (SIZE_T)size, &got)
                && got == (SIZE_T)size) {
                if (mask)
                    mask[i] = 1;
                ++ok;
            } else {
                if (mask)
                    mask[i] = 0;
                if (!g_last_error)
                    set_win_error();
            }
        }
    }

    while (done < workers - 1) {
        if (WaitForSingleObject(g_chunk_done, INFINITE) != WAIT_OBJECT_0)
            break;
        ++done;
    }

    LeaveCriticalSection(&g_pool_lock);
    return ok;
}

unsigned fastmem_pool_workers(void)
{
    unsigned n;

    if (!g_pool_ready)
        return 0;
    EnterCriticalSection(&g_pool_lock);
    n = g_pool_size;
    LeaveCriticalSection(&g_pool_lock);
    return n;
}

void fastmem_pool_shutdown(void)
{
    unsigned i;

    if (!g_pool_ready)
        return;
    EnterCriticalSection(&g_pool_lock);
    for (i = 0; i < g_pool_size; ++i) {
        fm_worker *w = &g_pool[i];
        if (w->thread) {
            InterlockedExchange(&w->shutdown, 1);
            SetEvent(w->wake);
            WaitForSingleObject(w->thread, 1000);
            CloseHandle(w->thread);
            w->thread = NULL;
        }
        if (w->wake) {
            CloseHandle(w->wake);
            w->wake = NULL;
        }
    }
    g_pool_size = 0;
    LeaveCriticalSection(&g_pool_lock);
}

/* ------------------------------------------------------------------ */
/* Exit                                                                */
/* ------------------------------------------------------------------ */

/*
 * Runs after main returns. Without it the worker threads would still be
 * parked in WaitForSingleObject when the process image goes away, which on
 * some Windows versions means the process does not exit.
 */
void __cdecl fastmem_atexit(void)
{
    fastmem_pool_shutdown();
}

/*
 * Registered from a DLL entry point when linked as a DLL. A static library
 * cannot be sure this runs, so callers embedding the .c file should also
 * call fastmem_pool_shutdown() explicitly.
 */
BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH)
        DisableThreadLibraryCalls(inst);
    else if (reason == DLL_PROCESS_DETACH)
        fastmem_pool_shutdown();
    return TRUE;
}