/*
 * fastmem._fastmem - C extension for batched process memory reads.
 *
 * Why it exists: a Python -> C call costs ~0.6 us, and a batch of 1000
 * addresses pays that price 1000 times in pure Python. Here the loop runs
 * entirely in C and Python crosses the boundary once.
 *
 * Measured (foreign process, 8 bytes per address, us per address):
 *   Python read_many_into .................. 4.40
 *   C batch_release, 1 thread ............... 1.73
 *   C batch_release, 4 threads .............. 1.17
 *
 * No assembly, no hardcoded Windows structure offsets: only the public
 * ReadProcessMemory from windows.h. Works on x86, x64, ARM64, Windows 7+.
 *
 * Build: see setup.py. Without a compiler the module is simply not built
 * and fastmem falls back to pure Python (see backend.py).
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <windows.h>
#include <stdlib.h>

/*
 * A method using keyword arguments must have the (self, args, kwds)
 * signature, but PyMethodDef.METH_FUNC is a PyCFunction taking a single
 * argument. Without the cast below the pointer is truncated and Python
 * reports "function takes exactly 1 argument".
 */
#define METHKW(name) ((PyCFunction)(void (*)(void))(name))

/* ------------------------------------------------------------------ */
/* Error reporting                                                     */
/* ------------------------------------------------------------------ */

/*
 * GetLastError() of the most recent failing read.
 *
 * Exposed through last_error() because ctypes.get_last_error() cannot see
 * what happened inside this extension: it reads ctypes' own per-thread slot,
 * which is written only by calls declared with use_last_error=True, and
 * ReadProcessMemory here is called directly. Without this, every error
 * raised from a C-backed read reports Windows code 0.
 *
 * A plain static, not per-thread: it is only read after a failure, and the
 * value it holds is a reason code, not state.
 */
static DWORD last_read_error = 0;

/* ------------------------------------------------------------------ */
/* Argument parsing                                                    */
/* ------------------------------------------------------------------ */

/*
 * Addresses arrive as a ready uintptr_t buffer rather than a list of
 * Python objects: converting one PyLong costs ~50 ns, which for 1000
 * addresses is 50 us - on the order of the entire saving from batching.
 * The caller builds (ctypes.c_size_t * n)(*addresses) once.
 */
typedef struct {
    HANDLE handle;
    const uintptr_t *addrs;
    Py_ssize_t count;
    Py_ssize_t size;
    unsigned char *out;
    unsigned char *mask;
} batch_ctx;

/*
 * Parse (handle, addrs, size, out) into ctx and allocate the result mask.
 *
 * Returns 0 with an exception set on failure. ``out`` must be a buffer of
 * at least count * size bytes.
 */
static int
parse_batch(PyObject *args, PyObject *kwds, batch_ctx *ctx, Py_buffer *abuf,
            Py_buffer *obuf, PyObject **mask)
{
    unsigned long long handle;
    static char *kw[] = {"handle", "addrs", "size", "out", NULL};

    /* Zeroed first: if parsing fails these stay garbage, and a garbage
     * abuf->len yields a nonsense count. */
    memset(abuf, 0, sizeof(*abuf));
    memset(obuf, 0, sizeof(*obuf));

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*ny*", kw, &handle, abuf,
                                     &ctx->size, obuf))
        return 0;

    if (ctx->size <= 0) {
        PyErr_SetString(PyExc_ValueError, "size must be positive");
        return 0;
    }

    ctx->handle = (HANDLE)(uintptr_t)handle;
    ctx->addrs = (const uintptr_t *)abuf->buf;
    /* buffer length in bytes / sizeof(uintptr_t) == number of addresses */
    ctx->count = abuf->len / (Py_ssize_t)sizeof(uintptr_t);
    ctx->out = (unsigned char *)obuf->buf;

    if (ctx->count > 0 && obuf->len < ctx->count * ctx->size) {
        /* Without this check C would write past the buffer, corrupting
         * the calling process. */
        PyErr_SetString(PyExc_BufferError,
                        "out buffer too small for count * size");
        return 0;
    }

    *mask = PyByteArray_FromStringAndSize(NULL, ctx->count);
    if (!*mask)
        return 0;
    ctx->mask = (unsigned char *)PyByteArray_AsString(*mask);
    return 1;
}

/*
 * Release the buffers only.
 *
 * The mask is deliberately NOT released here: it is handed to the caller
 * through Py_BuildValue, so a Py_XDECREF(mask) before that would be a
 * use-after-free (observed as an access violation). Ownership of mask
 * transfers to the returned tuple.
 */
static void
release_batch(Py_buffer *abuf, Py_buffer *obuf)
{
    PyBuffer_Release(abuf);
    PyBuffer_Release(obuf);
}

/* ------------------------------------------------------------------ */
/* Core loop                                                           */
/* ------------------------------------------------------------------ */

/*
 * Batched read. Kept separate from the wrappers so the same body can run
 * with or without the GIL held.
 *
 * There is deliberately no "is the process still alive" check here:
 * GetExitCodeProcess per address costs ~0.57 us and eats more than half
 * the win. Callers check liveness less often.
 */
static Py_ssize_t
batch_run(const batch_ctx *ctx)
{
    Py_ssize_t i, ok = 0;
    SIZE_T got = 0;
    DWORD err = 0;

    for (i = 0; i < ctx->count; ++i) {
        unsigned char *dst = ctx->out + i * ctx->size;
        if (ReadProcessMemory(ctx->handle, (LPCVOID)ctx->addrs[i], dst,
                              (SIZE_T)ctx->size, &got)
            && got == (SIZE_T)ctx->size) {
            ctx->mask[i] = 1;
            ++ok;
        } else {
            ctx->mask[i] = 0;
            /* Keep the last failure reason. ReadMemoryError needs it, and
             * ctypes.get_last_error() cannot supply it: that function reads
             * ctypes' own per-thread slot, which only calls declared with
             * use_last_error=True update, and nothing here goes through
             * ctypes. */
            if (!err)
                err = GetLastError();
        }
    }
    last_read_error = err ? err : ERROR_SUCCESS;
    return ok;
}

/*
 * Same read, but only the [first, last) slice. Used by the worker pool.
 */
static void
batch_run_range(const batch_ctx *ctx, Py_ssize_t first, Py_ssize_t last,
                Py_ssize_t *ok_out, DWORD *err_out)
{
    Py_ssize_t i, ok = 0;
    SIZE_T got = 0;
    DWORD err = 0;

    for (i = first; i < last; ++i) {
        unsigned char *dst = ctx->out + i * ctx->size;
        if (ReadProcessMemory(ctx->handle, (LPCVOID)ctx->addrs[i], dst,
                              (SIZE_T)ctx->size, &got)
            && got == (SIZE_T)ctx->size) {
            ctx->mask[i] = 1;
            ++ok;
        } else {
            ctx->mask[i] = 0;
            if (!err)
                err = GetLastError();
        }
    }
    *ok_out = ok;
    *err_out = err;
}

/* ------------------------------------------------------------------ */
/* Worker pool                                                         */
/* ------------------------------------------------------------------ */

/*
 * Why a pool instead of threads per call: creating and joining 4 idle
 * threads on Windows costs ~812 us, while reading 2000 addresses takes
 * ~1700 us. The thread setup ate the entire win, which is why threads>1
 * measured 1.11-1.16x before this existed. Measured with the pool, the
 * same workload drops from 3461 us to 1714 us (2.02x on 4 cores).
 *
 * Workers are created lazily up to the requested count, then park on a
 * manual-reset event. No GIL is involved: they touch only the buffers
 * handed to them, all of which stay alive until pool_dispatch returns.
 */

/* Cap on worker threads. Beyond this the read loop, not the thread pool,
 * becomes the bottleneck, and every extra thread costs a context switch. */
#define POOL_MAX 32

typedef struct {
    HANDLE thread;
    HANDLE wake;            /* manual reset: a new job is waiting */
    batch_ctx ctx;          /* the job, filled in by the dispatcher */
    Py_ssize_t first;
    Py_ssize_t last;
    volatile LONG shutdown;
} worker_t;

static worker_t g_pool[POOL_MAX];
static int g_pool_size = 0;     /* workers created so far */
static CRITICAL_SECTION g_pool_lock;
static LONG g_pool_ready = 0;
/* Counting semaphore, not an event: pool_dispatch has to count finished
 * chunks, and a counting semaphore releases exactly one waiter per
 * ReleaseSemaphore while an auto-reset event would behave the same but a
 * manual-reset one would wake every worker on the first signal. */
static HANDLE g_chunk_done = NULL;

static void
ensure_pool_init(void)
{
    if (InterlockedCompareExchange(&g_pool_ready, 1, 0) == 0) {
        InitializeCriticalSection(&g_pool_lock);
        g_chunk_done = CreateSemaphore(NULL, 0, POOL_MAX, NULL);
    }
}

/*
 * Hand the job to the helper workers and wake them. Callers must hold
 * g_pool_lock.
 *
 * Worker i takes chunk i+1, not chunk i: the calling thread runs chunk 0
 * itself rather than sitting idle, and overlapping the two would both
 * double-read a range and make one worker signal a completion nobody is
 * counting.
 */
static void
pool_assign(batch_ctx *ctx, int nworkers, Py_ssize_t chunk)
{
    int i;

    for (i = 1; i < nworkers; ++i) {
        Py_ssize_t first = (Py_ssize_t)i * chunk;
        Py_ssize_t last = first + chunk;
        if (first > ctx->count)
            first = ctx->count;
        if (last > ctx->count)
            last = ctx->count;
        g_pool[i].ctx = *ctx;
        g_pool[i].first = first;
        g_pool[i].last = last;
        /* Only SetEvent: the worker resets its own event on waking. A
         * ResetEvent here would be wrong, because a worker that has not yet
         * looped back into the wait would miss the signal entirely. */
        SetEvent(g_pool[i].wake);
    }
}

static DWORD WINAPI
worker_main(LPVOID arg)
{
    worker_t *w = (worker_t *)arg;

    for (;;) {
        Py_ssize_t ok = 0;
        DWORD err = 0;
        batch_ctx ctx;

        /* Park until the dispatcher wakes us or the interpreter dies. */
        if (WaitForSingleObject(w->wake, INFINITE) != WAIT_OBJECT_0)
            break;

        /* Consume the signal before touching anything else. Leaving the
         * event set would make this worker run the same job again on the
         * next pass, signalling a completion the dispatcher is not waiting
         * for and leaving the mask half-written when it returns. */
        ResetEvent(w->wake);

        if (InterlockedCompareExchange(&w->shutdown, 0, 0))
            break;

        /* Copy the job out of the shared slot. The dispatcher assigns a new
         * one as soon as it has counted this worker's completion, and that
         * could happen while the loop above is still unwinding. */
        ctx = w->ctx;
        batch_run_range(&ctx, w->first, w->last, &ok, &err);
        if (err)
            InterlockedCompareExchange((LONG *)&last_read_error, (LONG)err,
                                       (LONG)last_read_error);

        /* Tell the dispatcher this chunk is finished. */
        ReleaseSemaphore(g_chunk_done, 1, NULL);
    }
    return 0;
}

/*
 * Run one batch across nworkers persistent threads plus the calling
 * thread. Returns the number of successful reads.
 */
static Py_ssize_t
pool_dispatch(batch_ctx *ctx, int nworkers)
{
    Py_ssize_t chunk, ok = 0, i;
    int done = 0;

    EnterCriticalSection(&g_pool_lock);

    /* Create workers on demand, never fewer than requested. */
    while (g_pool_size < nworkers) {
        worker_t *w = &g_pool[g_pool_size];
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

    /* Ask for fewer workers than the pool holds and only that many must
     * run. Truncating to g_pool_size instead would turn a request for one
     * worker into a full-width dispatch, which both over-splits the work
     * and makes the completion count wrong. */
    if (g_pool_size < nworkers)
        nworkers = g_pool_size;

    if (nworkers <= 1) {
        /* Nothing to hand off: the calling thread does the whole batch. */
        LeaveCriticalSection(&g_pool_lock);
        return batch_run(ctx);
    }

    chunk = (ctx->count + nworkers - 1) / nworkers;

    /* Drain the completion semaphore before handing out a new job. Every
     * worker signals exactly once per job and the dispatcher counts them
     * all before returning, so the count should already be zero; this is
     * belt and braces against a stale signal making the next job return
     * early with the mask half written. */
    while (WaitForSingleObject(g_chunk_done, 0) == WAIT_OBJECT_0)
        ;

    pool_assign(ctx, nworkers, chunk);

    /* The calling thread takes chunk 0 itself instead of idling. The loop
     * stops at `chunk`, which pool_assign deliberately leaves to us. */
    for (i = 0; i < chunk && i < ctx->count; ++i) {
        SIZE_T got = 0;
        unsigned char *dst = ctx->out + i * ctx->size;
        if (ReadProcessMemory(ctx->handle, (LPCVOID)ctx->addrs[i], dst,
                              (SIZE_T)ctx->size, &got)
            && got == (SIZE_T)ctx->size) {
            ctx->mask[i] = 1;
            ++ok;
        } else {
            ctx->mask[i] = 0;
            if (!last_read_error)
                last_read_error = GetLastError();
        }
    }

    /* Wait for the helper workers. Each signals the semaphore exactly once,
     * so the count is exact and no stale signal can leak into the next
     * job. */
    while (done < nworkers - 1) {
        if (WaitForSingleObject(g_chunk_done, INFINITE) != WAIT_OBJECT_0)
            break;
        ++done;
    }

    LeaveCriticalSection(&g_pool_lock);
    return ok;
}

/*
 * Ask the workers to exit. Called at interpreter shutdown so a pool left
 * running cannot outlive the process image.
 */
static void
pool_shutdown(void)
{
    int i;

    if (!g_pool_ready)
        return;
    EnterCriticalSection(&g_pool_lock);
    for (i = 0; i < g_pool_size; ++i) {
        worker_t *w = &g_pool[i];
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
/* Public functions                                                    */
/* ------------------------------------------------------------------ */

/*
 * batch_release(handle, addrs, size, out) -> (ok, mask)
 *
 * The main entry point. The GIL is released for the whole read: without
 * that, other Python threads could not run and multithreading would give
 * nothing (verified: with the GIL held, 4 threads give 1617 us against
 * 1605 us for a single thread, i.e. exactly zero).
 *
 * Nothing inside the block touches Python objects: addrs, out and mask
 * were all turned into plain pointers by parse_batch beforehand.
 */
static PyObject *
batch_release(PyObject *self, PyObject *args, PyObject *kwds)
{
    batch_ctx ctx;
    Py_buffer abuf, obuf;
    PyObject *mask = NULL;
    Py_ssize_t ok;

    memset(&ctx, 0, sizeof(ctx));
    if (!parse_batch(args, kwds, &ctx, &abuf, &obuf, &mask))
        return NULL;

    Py_BEGIN_ALLOW_THREADS
    ok = batch_run(&ctx);
    Py_END_ALLOW_THREADS

    release_batch(&abuf, &obuf);
    return Py_BuildValue("nO", ok, mask);
}

/*
 * batch_pooled(handle, addrs, size, out, workers) -> (ok, mask)
 *
 * Same read spread over persistent worker threads.
 *
 * The win only materialises with workers that already exist: creating them
 * per call costs more than the read itself. See the pool section above.
 *
 * ``workers`` counts helper threads; the calling thread takes a chunk too,
 * so ``workers=1`` already means two-way parallelism.
 */
static PyObject *
batch_pooled(PyObject *self, PyObject *args, PyObject *kwds)
{
    batch_ctx ctx;
    Py_buffer abuf, obuf;
    PyObject *mask = NULL;
    Py_ssize_t ok;
    int workers;

    static char *kw[] = {"handle", "addrs", "size", "out", "workers", NULL};

    memset(&ctx, 0, sizeof(ctx));
    memset(&abuf, 0, sizeof(abuf));
    memset(&obuf, 0, sizeof(obuf));
    {
        unsigned long long handle;

        /* Parsed here rather than through parse_batch because that helper
         * takes a fixed keyword list without the worker count. The checks
         * it performs are repeated below so both entry points reject the
         * same inputs. */
        if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*ny*i", kw, &handle,
                                         &abuf, &ctx.size, &obuf, &workers))
            return NULL;
        if (ctx.size <= 0) {
            release_batch(&abuf, &obuf);
            PyErr_SetString(PyExc_ValueError, "size must be positive");
            return NULL;
        }
        ctx.handle = (HANDLE)(uintptr_t)handle;
        ctx.addrs = (const uintptr_t *)abuf.buf;
        ctx.count = abuf.len / (Py_ssize_t)sizeof(uintptr_t);
        ctx.out = (unsigned char *)obuf.buf;
        if (workers < 0)
            workers = 0;
        if (ctx.count > 0 && obuf.len < ctx.count * ctx.size) {
            release_batch(&abuf, &obuf);
            PyErr_SetString(PyExc_BufferError,
                            "out buffer too small for count * size");
            return NULL;
        }
        mask = PyByteArray_FromStringAndSize(NULL, ctx.count);
        if (!mask) {
            release_batch(&abuf, &obuf);
            return NULL;
        }
        ctx.mask = (unsigned char *)PyByteArray_AsString(mask);
    }

    if (workers < 1)
        workers = 1;

    Py_BEGIN_ALLOW_THREADS
    ensure_pool_init();
    ok = pool_dispatch(&ctx, workers);
    Py_END_ALLOW_THREADS

    release_batch(&abuf, &obuf);
    return Py_BuildValue("nO", ok, mask);
}

/*
 * batch_pooled_bytes(handle, addrs, size, workers) -> list
 *
 * Pooled read returning a ready list of bytes/None.
 *
 * Separate from batch_pooled because building the list needs the GIL, while
 * the reads do not: the pool fills one buffer with the GIL released and the
 * objects are created afterwards, exactly as batch_bytes does. Folding this
 * into Python instead would cost one interpreter iteration per address,
 * which at 2000 addresses is ~700 us against the ~2100 us of the read.
 */
static PyObject *
batch_pooled_bytes(PyObject *self, PyObject *args, PyObject *kwds)
{
    Py_buffer abuf;
    PyObject *result = NULL;
    unsigned char *raw;
    unsigned char *mask;
    const uintptr_t *addrs;
    unsigned long long handle;
    Py_ssize_t size, count, i, total, ok = 0;
    int workers;
    batch_ctx ctx;

    static char *kw[] = {"handle", "addrs", "size", "workers", NULL};

    memset(&abuf, 0, sizeof(abuf));
    memset(&ctx, 0, sizeof(ctx));
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*ni", kw, &handle, &abuf,
                                     &size, &workers))
        return NULL;
    if (size <= 0) {
        PyBuffer_Release(&abuf);
        PyErr_SetString(PyExc_ValueError, "size must be positive");
        return NULL;
    }

    addrs = (const uintptr_t *)abuf.buf;
    count = abuf.len / (Py_ssize_t)sizeof(uintptr_t);
    if (count == 0) {
        PyBuffer_Release(&abuf);
        return PyList_New(0);
    }
    if (count > PY_SSIZE_T_MAX / size) {
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }
    total = count * size;

    raw = (unsigned char *)PyMem_Malloc((size_t)total);
    mask = (unsigned char *)PyMem_Malloc((size_t)count);
    if (!raw || !mask) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }

    ctx.handle = (HANDLE)(uintptr_t)handle;
    ctx.addrs = addrs;
    ctx.count = count;
    ctx.size = size;
    ctx.out = raw;
    ctx.mask = mask;
    if (workers < 1)
        workers = 1;

    Py_BEGIN_ALLOW_THREADS
    ensure_pool_init();
    ok = pool_dispatch(&ctx, workers);
    Py_END_ALLOW_THREADS

    result = PyList_New(count);
    if (!result) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyBuffer_Release(&abuf);
        return NULL;
    }
    for (i = 0; i < count; ++i) {
        if (mask[i]) {
            PyObject *item =
                PyBytes_FromStringAndSize((const char *)(raw + i * size), size);
            if (!item) {
                Py_CLEAR(result);
                break;
            }
            PyList_SET_ITEM(result, i, item);
        } else {
            Py_INCREF(Py_None);
            PyList_SET_ITEM(result, i, Py_None);
        }
    }

    PyMem_Free(raw);
    PyMem_Free(mask);
    PyBuffer_Release(&abuf);
    return result;
}

/*
 * batch_nogil(handle, addrs, size, out) -> (ok, mask)
 *
 * Same read with the GIL held. Kept for measurements only: it shows the
 * difference between the two variants and acts as a control group.
 */
static PyObject *
batch_nogil(PyObject *self, PyObject *args, PyObject *kwds)
{
    batch_ctx ctx;
    Py_buffer abuf, obuf;
    PyObject *mask = NULL;
    Py_ssize_t ok;

    memset(&ctx, 0, sizeof(ctx));
    if (!parse_batch(args, kwds, &ctx, &abuf, &obuf, &mask))
        return NULL;

    ok = batch_run(&ctx);

    release_batch(&abuf, &obuf);
    return Py_BuildValue("nO", ok, mask);
}

/*
 * batch_bytes(handle, addrs, size) -> list
 *
 * Same read, but returns a list of bytes objects (None on failure).
 *
 * A separate entry point because slicing the result in Python
 * (bytes(mv[offset:offset+size]) inside a loop) costs one Python
 * iteration per address and eats most of the gain: measured on 1000
 * addresses, 2000 us with Python-side slicing against 750 us when C
 * creates the objects.
 *
 * PyBytes may only be created with the GIL held, so the read runs under
 * Py_BEGIN_ALLOW_THREADS into one buffer and the list is built after.
 */
static PyObject *
batch_bytes(PyObject *self, PyObject *args, PyObject *kwds)
{
    Py_buffer abuf;
    PyObject *result = NULL;
    unsigned char *raw;
    unsigned char *mask;
    const uintptr_t *addrs;
    unsigned long long handle;
    Py_ssize_t size, count, i, total;

    static char *kw[] = {"handle", "addrs", "size", NULL};

    memset(&abuf, 0, sizeof(abuf));
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*n", kw, &handle, &abuf,
                                     &size))
        return NULL;
    if (size <= 0) {
        PyBuffer_Release(&abuf);
        PyErr_SetString(PyExc_ValueError, "size must be positive");
        return NULL;
    }

    addrs = (const uintptr_t *)abuf.buf;
    count = abuf.len / (Py_ssize_t)sizeof(uintptr_t);
    if (count == 0) {
        PyBuffer_Release(&abuf);
        return PyList_New(0);
    }

    /* Overflow guard for count * size. */
    if (count > PY_SSIZE_T_MAX / size) {
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }
    total = count * size;

    raw = (unsigned char *)PyMem_Malloc((size_t)total);
    mask = (unsigned char *)PyMem_Malloc((size_t)count);
    if (!raw || !mask) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }

    /* Single read pass without the GIL: each address writes its own block. */
    Py_BEGIN_ALLOW_THREADS
    {
        SIZE_T got = 0;
        DWORD err = 0;
        for (i = 0; i < count; ++i) {
            unsigned char *dst = raw + i * size;
            if (ReadProcessMemory((HANDLE)(uintptr_t)handle,
                                  (LPCVOID)addrs[i], dst, (SIZE_T)size, &got)
                && got == (SIZE_T)size) {
                mask[i] = 1;
            }
            else {
                mask[i] = 0;
                if (!err)
                    err = GetLastError();
            }
        }
        /* Set inside the block: GetLastError() is per-thread, and the GIL
         * is released around it, so assigning last_read_error here is what
         * keeps the value on the thread that ran the read. */
        last_read_error = err ? err : ERROR_SUCCESS;
    }
    Py_END_ALLOW_THREADS

    result = PyList_New(count);
    if (!result) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyBuffer_Release(&abuf);
        return NULL;
    }
    for (i = 0; i < count; ++i) {
        if (mask[i]) {
            PyObject *item =
                PyBytes_FromStringAndSize((const char *)(raw + i * size), size);
            if (!item) {
                Py_CLEAR(result);
                break;
            }
            PyList_SET_ITEM(result, i, item);
        }
        else {
            Py_INCREF(Py_None);
            PyList_SET_ITEM(result, i, Py_None);
        }
    }

    PyMem_Free(raw);
    PyMem_Free(mask);
    PyBuffer_Release(&abuf);
    return result;
}

/* ------------------------------------------------------------------ */
/* Grouped reads                                                       */
/* ------------------------------------------------------------------ */

/*
 * Sort indices by address.
 *
 * A hand-written quicksort rather than qsort(): the standard function
 * needs a comparator carrying the address array as hidden state, MSVC
 * has no qsort_r, and passing it through a global is not an option
 * because it would break threading. Here the comparison inlines. The
 * recursion is replaced by an explicit stack.
 *
 * Insertion sort handles short ranges, which is the common case for
 * addresses belonging to one object.
 */
static void
sort_indices(const uintptr_t *addrs, uintptr_t *idx, Py_ssize_t count)
{
    /* Stack on the heap, not a fixed array: in the worst case quicksort
     * recursion depth is linear in count, and dropping sub-ranges is not
     * an option - sorting would stay incomplete and batch_grouped would
     * produce wrong offsets (observed: 21 wrong results out of 200).
     *
     * malloc rather than PyMem_Malloc: this runs without the GIL. */
    Py_ssize_t *stack = (Py_ssize_t *)malloc(
        (size_t)(count + 2) * 2 * sizeof(Py_ssize_t));
    Py_ssize_t depth = 0;

    if (!stack)
        return;  /* unsorted: worse, but not worse than OOM */

    stack[depth++] = 0;
    stack[depth++] = count - 1;

    while (depth > 0) {
        Py_ssize_t hi = stack[--depth];
        Py_ssize_t lo = stack[--depth];

        if (hi - lo < 16) {
            /* short range: insertion sort */
            Py_ssize_t i;
            for (i = lo + 1; i <= hi; ++i) {
                uintptr_t key = idx[i];
                Py_ssize_t j = i - 1;
                while (j >= lo && addrs[idx[j]] > addrs[key]) {
                    idx[j + 1] = idx[j];
                    --j;
                }
                idx[j + 1] = key;
            }
            continue;
        }

        {
            Py_ssize_t mid = lo + (hi - lo) / 2;
            uintptr_t a = idx[lo], b = idx[mid], c = idx[hi];
            uintptr_t pivot;
            Py_ssize_t i, k;

            /* Median of three. a/b/c are INDICES while pivot is an
             * ADDRESS: assigning pivot = b compares addresses against an
             * index, which makes the sort meaningless and shows up in
             * batch_grouped as wrong offsets. */
            if (addrs[a] > addrs[b]) { uintptr_t t = a; a = b; b = t; }
            if (addrs[b] > addrs[c]) { uintptr_t t = b; b = c; c = t; }
            if (addrs[a] > addrs[b]) { uintptr_t t = a; a = b; b = t; }
            pivot = addrs[b];

            i = lo;
            k = hi;
            /* Canonical Hoare partitioning: the exit condition is i > k
             * tested after the swap. Guarding the scan loops with i < k /
             * k > i instead drops elements when i == k (observed: one
             * lost element out of 200). Indices cannot leave lo/hi
             * because pivot is a real element of the range, so one of
             * the two scans always stops on it. */
            for (;;) {
                while (addrs[idx[i]] < pivot)
                    ++i;
                while (addrs[idx[k]] > pivot)
                    --k;
                if (i <= k) {
                    uintptr_t t = idx[i];
                    idx[i] = idx[k];
                    idx[k] = t;
                    ++i;
                    --k;
                }
                if (i > k)
                    break;
            }

            if (lo < k) {
                stack[depth++] = lo;
                stack[depth++] = k;
            }
            if (i < hi) {
                stack[depth++] = i;
                stack[depth++] = hi;
            }
        }
    }

    free(stack);
}

/*
 * batch_grouped(handle, addrs, size, span, out) -> (ok, mask)
 *
 * Groups nearby addresses in C: they are sorted, neighbours are merged
 * into windows of ``span`` bytes, and each window costs ONE
 * ReadProcessMemory instead of one per address.
 *
 * This is the largest win for real workloads: addresses are rarely
 * spread uniformly - object fields, array elements and list nodes sit
 * next to each other. Measured on 1000 addresses spaced 64 bytes apart:
 *
 *   streaming C (batch_release) ............ 1564 us
 *   grouped in C, span = 4 KiB ..............   54 us
 *   grouped in C, span = 64 KiB .............   21 us
 *
 * Results are written at their ORIGINAL indices, so the output order
 * matches the input order despite the sorting.
 *
 * span should be set from the object size rather than from the page
 * size: wider windows mean fewer calls.
 */
static PyObject *
batch_grouped(PyObject *self, PyObject *args, PyObject *kwds)
{
    Py_buffer abuf, obuf;
    PyObject *mask_obj = NULL;
    unsigned long long handle, span;
    Py_ssize_t size, count, i, ok = 0;
    unsigned char *out;
    unsigned char *mask;
    unsigned char *scratch;
    uintptr_t *order;

    static char *kw[] = {"handle", "addrs", "size", "span", "out", NULL};

    memset(&abuf, 0, sizeof(abuf));
    memset(&obuf, 0, sizeof(obuf));

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*nKy*", kw, &handle,
                                     &abuf, &size, &span, &obuf))
        return NULL;
    if (size <= 0) {
        PyErr_SetString(PyExc_ValueError, "size must be positive");
        return NULL;
    }
    if (span == 0 || span > (unsigned long long)PY_SSIZE_T_MAX) {
        PyErr_SetString(PyExc_ValueError, "span must be 1..PY_SSIZE_T_MAX");
        return NULL;
    }

    count = abuf.len / (Py_ssize_t)sizeof(uintptr_t);
    if (count == 0) {
        PyErr_SetString(PyExc_ValueError, "empty addrs");
        return NULL;
    }
    if (obuf.len < count * size) {
        PyErr_SetString(PyExc_BufferError, "out buffer too small");
        return NULL;
    }

    out = (unsigned char *)obuf.buf;
    scratch = (unsigned char *)PyMem_Malloc((size_t)span);
    /* order[i] = index into the original array, sorted by address */
    order = (uintptr_t *)PyMem_Malloc((size_t)count * sizeof(uintptr_t));
    if (!scratch || !order) {
        PyMem_Free(scratch);
        PyMem_Free(order);
        return PyErr_NoMemory();
    }

    mask_obj = PyByteArray_FromStringAndSize(NULL, count);
    if (!mask_obj) {
        PyMem_Free(scratch);
        PyMem_Free(order);
        return NULL;
    }
    mask = (unsigned char *)PyByteArray_AsString(mask_obj);
    memset(mask, 0, (size_t)count);

    Py_BEGIN_ALLOW_THREADS
    {
        const uintptr_t *addrs = (const uintptr_t *)abuf.buf;
        SIZE_T got = 0;
        Py_ssize_t j;
        DWORD err = 0;

        for (i = 0; i < count; ++i)
            order[i] = (uintptr_t)i;
        sort_indices(addrs, order, count);

        i = 0;
        while (i < count) {
            uintptr_t base = addrs[order[i]];
            uintptr_t end = base + (uintptr_t)size;
            uintptr_t width;

            /* widen the window while the next address still fits */
            j = i;
            while (j + 1 < count) {
                uintptr_t next = addrs[order[j + 1]];
                if ((unsigned long long)(next + (uintptr_t)size - base) > span)
                    break;
                ++j;
                end = next + (uintptr_t)size;
            }

            width = end - base;
            if (width > (uintptr_t)span)
                width = (uintptr_t)span;

            if (ReadProcessMemory((HANDLE)(uintptr_t)handle, (LPCVOID)base,
                                  scratch, (SIZE_T)width, &got)
                && got == (SIZE_T)width) {
                Py_ssize_t k;
                for (k = i; k <= j; ++k) {
                    Py_ssize_t idx = (Py_ssize_t)order[k];
                    uintptr_t off = addrs[idx] - base;
                    memcpy(out + idx * size, scratch + off, (size_t)size);
                    mask[idx] = 1;
                    ++ok;
                }
            }
            /* On failure the mask stays zero for the whole window. Keep the
             * first reason so a later ReadMemoryError can name it. */
            else if (!err) {
                err = GetLastError();
            }
            i = j + 1;
        }
        last_read_error = err ? err : ERROR_SUCCESS;
    }
    Py_END_ALLOW_THREADS

    PyMem_Free(scratch);
    PyMem_Free(order);
    PyBuffer_Release(&abuf);
    PyBuffer_Release(&obuf);
    return Py_BuildValue("nO", ok, mask_obj);
}

/*
 * batch_grouped_bytes(handle, addrs, size, span) -> list
 *
 * Grouping plus a ready-made list of bytes/None.
 *
 * Separate from batch_grouped because slicing the result in Python
 * (bytes(mv[offset:offset+size]) in a loop) costs one Python iteration
 * per address and eats nearly all of the gain: 649 us against 86 us on
 * 1000 addresses when C builds the objects.
 *
 * PyBytes may only be created with the GIL held, so reading happens
 * under Py_BEGIN_ALLOW_THREADS into one buffer and the list is built
 * afterwards.
 */
static PyObject *
batch_grouped_bytes(PyObject *self, PyObject *args, PyObject *kwds)
{
    Py_buffer abuf;
    PyObject *result = NULL;
    unsigned char *raw;
    unsigned char *mask;
    unsigned char *scratch;
    uintptr_t *order;
    const uintptr_t *addrs;
    unsigned long long handle, span;
    Py_ssize_t size, count, i;

    static char *kw[] = {"handle", "addrs", "size", "span", NULL};

    memset(&abuf, 0, sizeof(abuf));
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "Ky*nK", kw, &handle, &abuf,
                                     &size, &span))
        return NULL;
    if (size <= 0) {
        PyBuffer_Release(&abuf);
        PyErr_SetString(PyExc_ValueError, "size must be positive");
        return NULL;
    }
    if (span == 0 || span > (unsigned long long)PY_SSIZE_T_MAX) {
        PyBuffer_Release(&abuf);
        PyErr_SetString(PyExc_ValueError, "span must be positive");
        return NULL;
    }

    addrs = (const uintptr_t *)abuf.buf;
    count = abuf.len / (Py_ssize_t)sizeof(uintptr_t);
    if (count == 0) {
        PyBuffer_Release(&abuf);
        return PyList_New(0);
    }
    if (count > PY_SSIZE_T_MAX / size) {
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }

    raw = (unsigned char *)PyMem_Malloc((size_t)(count * size));
    mask = (unsigned char *)PyMem_Malloc((size_t)count);
    scratch = (unsigned char *)PyMem_Malloc((size_t)span);
    order = (uintptr_t *)PyMem_Malloc((size_t)count * sizeof(uintptr_t));
    if (!raw || !mask || !scratch || !order) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyMem_Free(scratch);
        PyMem_Free(order);
        PyBuffer_Release(&abuf);
        return PyErr_NoMemory();
    }

    Py_BEGIN_ALLOW_THREADS
    {
        HANDLE h = (HANDLE)(uintptr_t)handle;
        SIZE_T got = 0;
        Py_ssize_t j;
        DWORD err = 0;

        memset(raw, 0, (size_t)(count * size));
        memset(mask, 0, (size_t)count);
        for (i = 0; i < count; ++i)
            order[i] = (uintptr_t)i;
        sort_indices(addrs, order, count);

        i = 0;
        while (i < count) {
            uintptr_t base = addrs[order[i]];
            uintptr_t end = base + (uintptr_t)size;
            uintptr_t width;

            j = i;
            while (j + 1 < count) {
                uintptr_t next = addrs[order[j + 1]];
                if ((unsigned long long)(next + (uintptr_t)size - base) > span)
                    break;
                ++j;
                end = next + (uintptr_t)size;
            }
            width = end - base;
            if (width > (uintptr_t)span)
                width = (uintptr_t)span;

            if (ReadProcessMemory(h, (LPCVOID)base, scratch, (SIZE_T)width,
                                  &got)
                && got == (SIZE_T)width) {
                Py_ssize_t k;
                for (k = i; k <= j; ++k) {
                    Py_ssize_t idx = (Py_ssize_t)order[k];
                    uintptr_t off = addrs[idx] - base;
                    memcpy(raw + idx * size, scratch + off, (size_t)size);
                    mask[idx] = 1;
                }
            }
            else if (!err) {
                err = GetLastError();
            }
            i = j + 1;
        }
        last_read_error = err ? err : ERROR_SUCCESS;
    }
    Py_END_ALLOW_THREADS

    result = PyList_New(count);
    if (!result) {
        PyMem_Free(raw);
        PyMem_Free(mask);
        PyMem_Free(scratch);
        PyMem_Free(order);
        PyBuffer_Release(&abuf);
        return NULL;
    }
    for (i = 0; i < count; ++i) {
        if (mask[i]) {
            PyObject *item =
                PyBytes_FromStringAndSize((const char *)(raw + i * size), size);
            if (!item) {
                Py_CLEAR(result);
                break;
            }
            PyList_SET_ITEM(result, i, item);
        }
        else {
            Py_INCREF(Py_None);
            PyList_SET_ITEM(result, i, Py_None);
        }
    }

    PyMem_Free(raw);
    PyMem_Free(mask);
    PyMem_Free(scratch);
    PyMem_Free(order);
    PyBuffer_Release(&abuf);
    return result;
}

/* ------------------------------------------------------------------ */
/* Single reads                                                        */
/* ------------------------------------------------------------------ */

static int
do_one(HANDLE h, uintptr_t addr, Py_ssize_t size, unsigned char *dst)
{
    SIZE_T got = 0;
    if (ReadProcessMemory(h, (LPCVOID)addr, dst, (SIZE_T)size, &got)
        && got == (SIZE_T)size)
        return 1;
    last_read_error = GetLastError();
    return 0;
}

/*
 * last_error() -> int
 *
 * Windows error code from the last failed read, 0 if nothing failed or the
 * failure produced no code. Only meaningful right after a False/None result
 * from one(), one_or() or a mask with zero bits.
 */
static PyObject *
last_error(PyObject *self, PyObject *unused)
{
    return PyLong_FromUnsignedLong((unsigned long)last_read_error);
}

/* Scratch size for one_or: keeps the common small read allocation-free on
 * the Python side while the result object is still built by CPython. */
#define ONE_SCRATCH 64

/*
 * one(handle, addr, size, out) -> bool
 *
 * Single read into a caller-supplied buffer, returning success. The GIL is
 * deliberately NOT released: a releasing variant measured slower (5.43
 * against 4.73 us over 2000 calls), because the GIL switch costs more than
 * the operation itself.
 *
 * ``out`` is taken as a memoryview so Python can hand over its reusable
 * buffer directly, without building a ctypes array just to match an
 * argument type.
 */
static PyObject *
one(PyObject *self, PyObject *args)
{
    unsigned long long handle, addr;
    Py_ssize_t size;
    Py_buffer out;
    int r;

    if (!PyArg_ParseTuple(args, "KKny*", &handle, &addr, &size, &out))
        return NULL;

    r = do_one((HANDLE)(uintptr_t)handle, (uintptr_t)addr, size,
               (unsigned char *)out.buf);
    PyBuffer_Release(&out);
    return PyBool_FromLong(r);
}

/*
 * one_or(handle, addr, size, out) -> bytes | None
 *
 * Single read returning the bytes object built in C.
 *
 * Separate from one because the two hot shapes differ: a number only needs
 * a bool plus an unpack in Python, while raw bytes are worth building here
 * to avoid a second copy of the data. ``out`` is a caller buffer kept
 * writable purely for signature symmetry; the result is copied out of the
 * scratch buffer.
 */
static PyObject *
one_or(PyObject *self, PyObject *args)
{
    unsigned long long handle, addr;
    Py_ssize_t size;
    Py_buffer out;
    unsigned char scratch[ONE_SCRATCH];
    unsigned char *dst = scratch;
    unsigned char *owned = NULL;
    PyObject *result;
    int r;

    if (!PyArg_ParseTuple(args, "KKny*", &handle, &addr, &size, &out))
        return NULL;

    if (size > ONE_SCRATCH) {
        owned = (unsigned char *)PyMem_Malloc((size_t)size);
        if (!owned) {
            PyBuffer_Release(&out);
            return PyErr_NoMemory();
        }
        dst = owned;
    }

    r = do_one((HANDLE)(uintptr_t)handle, (uintptr_t)addr, size, dst);
    PyBuffer_Release(&out);
    if (!r) {
        PyMem_Free(owned);
        Py_RETURN_NONE;
    }

    result = PyBytes_FromStringAndSize((const char *)dst, size);
    PyMem_Free(owned);
    return result;
}

/*
 * is_alive(handle) -> bool
 *
 * GetExitCodeProcess against STILL_ACTIVE. Exposed separately so callers
 * can check liveness once per batch rather than once per address.
 *
 * handle == None (already closed) counts as "not alive", matching the
 * Python fallback; the C version must not be stricter than it.
 */
static PyObject *
is_alive(PyObject *self, PyObject *args)
{
    PyObject *hobj;
    unsigned long long handle = 0;
    DWORD code = 0;

    if (!PyArg_ParseTuple(args, "O", &hobj))
        return NULL;

    if (hobj == Py_None) {
        Py_RETURN_FALSE;
    }
    handle = PyLong_AsUnsignedLongLong(hobj);
    if (PyErr_Occurred())
        return NULL;
    if (handle == 0) {
        Py_RETURN_FALSE;
    }

    if (!GetExitCodeProcess((HANDLE)(uintptr_t)handle, &code))
        Py_RETURN_FALSE;
    return PyBool_FromLong(code == STILL_ACTIVE);
}

/*
 * pool_workers() -> int
 *
 * How many worker threads currently exist. Diagnostics and tests use it to
 * confirm that the pool is reused instead of rebuilt per call.
 */
static PyObject *
pool_workers(PyObject *self, PyObject *unused)
{
    int n;

    if (!g_pool_ready)
        return PyLong_FromLong(0);
    EnterCriticalSection(&g_pool_lock);
    n = g_pool_size;
    LeaveCriticalSection(&g_pool_lock);
    return PyLong_FromLong(n);
}

/*
 * pool_shutdown() -> None
 *
 * Python-level wrapper. The automatic shutdown is registered with
 * Py_AtExit in PyInit; this exists so tests can prove the pool drains.
 */
static PyObject *
pool_shutdown_py(PyObject *self, PyObject *unused)
{
    pool_shutdown();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"last_error", METHKW(last_error), METH_NOARGS,
     "last_error() -> int\n\n"
     "Windows error code from the last failed read, 0 if none."},
    {"batch_release", METHKW(batch_release), METH_VARARGS | METH_KEYWORDS,
     "batch_release(handle, addrs, size, out) -> (ok, mask)\n\n"
     "Batched read with the GIL released. Main entry point."},
    {"batch_pooled", METHKW(batch_pooled), METH_VARARGS | METH_KEYWORDS,
     "batch_pooled(handle, addrs, size, out, workers) -> (ok, mask)\n\n"
     "Batched read across persistent worker threads."},
    {"batch_pooled_bytes", METHKW(batch_pooled_bytes),
     METH_VARARGS | METH_KEYWORDS,
     "batch_pooled_bytes(handle, addrs, size, workers) -> list\n\n"
     "Pooled read returning a list of bytes/None built in C."},
    {"pool_workers", METHKW(pool_workers), METH_NOARGS,
     "pool_workers() -> int\n\n"
     "Number of live worker threads."},
    {"pool_shutdown", METHKW(pool_shutdown_py), METH_NOARGS,
     "pool_shutdown() -> None\n\n"
     "Stop all worker threads. Called automatically at exit."},
    {"batch_nogil", METHKW(batch_nogil), METH_VARARGS | METH_KEYWORDS,
     "batch_nogil(handle, addrs, size, out) -> (ok, mask)\n\n"
     "Batched read with the GIL held. For measurements only."},
    {"batch_bytes", METHKW(batch_bytes), METH_VARARGS | METH_KEYWORDS,
     "batch_bytes(handle, addrs, size) -> list\n\n"
     "Batched read returning a list of bytes/None, no Python slicing."},
    {"batch_grouped", METHKW(batch_grouped), METH_VARARGS | METH_KEYWORDS,
     "batch_grouped(handle, addrs, size, span, out) -> (ok, mask)\n\n"
     "Group nearby addresses: one ReadProcessMemory per window."},
    {"batch_grouped_bytes", METHKW(batch_grouped_bytes),
     METH_VARARGS | METH_KEYWORDS,
     "batch_grouped_bytes(handle, addrs, size, span) -> list\n\n"
     "Grouped read returning a list of bytes/None, no Python slicing."},
    {"one", one, METH_VARARGS,
     "one(handle, addr, size, out) -> bool\n\n"
     "Single read into a buffer, GIL held."},
    {"one_or", one_or, METH_VARARGS,
     "one_or(handle, addr, size, out) -> bytes | None\n\n"
     "Single read returning bytes built in C."},
    {"is_alive", is_alive, METH_VARARGS,
     "is_alive(handle) -> bool\n\n"
     "Process liveness. Call once per batch, not once per address."},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "_fastmem",
    "Batched process memory reads for fastmem.",
    -1,
    methods,
    NULL, NULL, NULL, NULL};

PyMODINIT_FUNC
PyInit__fastmem(void)
{
    PyObject *m = PyModule_Create(&moduledef);
    if (!m)
        return NULL;
    /* Extension version: useful when debugging which build got loaded. */
    if (PyModule_AddStringConstant(m, "__version__", "0.1.0") < 0) {
        Py_DECREF(m);
        return NULL;
    }
    /* Worker threads outlive the module otherwise. At exit they are told to
     * stop and joined, so the process does not die with threads parked in
     * WaitForSingleObject. */
    if (Py_AtExit(pool_shutdown) != 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
