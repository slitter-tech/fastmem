/*
 * fastmem.hpp - header-only C++17 wrapper over fastmem.h.
 *
 * Adds the parts C deliberately leaves out: RAII for the handle, typed
 * reads, std::vector containers, and exceptions. The performance-critical
 * paths stay a direct call into the C layer, so wrapping costs a
 * predictable few tens of nanoseconds rather than hiding the loop.
 *
 *     #include <fastmem/fastmem.hpp>
 *
 *     fastmem::Process p(1234);
 *     auto hp = p.read<uintptr_t>(addr);
 *
 * Link against fastmem.lib (DLL) or compile csrc/fastmem.c directly with
 * -DFASTMEM_STATIC.
 */

#ifndef FASTMEM_HPP
#define FASTMEM_HPP

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <system_error>
#include <type_traits>
#include <utility>
#include <vector>

#include "fastmem.h"

namespace fastmem {

/* ------------------------------------------------------------------ */
/* Errors                                                              */
/* ------------------------------------------------------------------ */

/*
 * Thrown by the throwing helpers. Carries the Windows code so a caller can
 * branch on it without parsing the message.
 */
class error : public std::runtime_error {
public:
    error(unsigned long code, const std::string &what)
        : std::runtime_error(what), code_(code) {}

    /* Windows error code, or one of the FM_ERR_* values. */
    unsigned long code() const noexcept { return code_; }

private:
    unsigned long code_;
};

namespace detail {

[[noreturn]] inline void fail(const char *what) {
    const unsigned long code = fastmem_last_error();
    throw error(code, std::string(what) + ": " + fastmem_strerror(code));
}

/*
 * Read `size` bytes into a T. Rejects a size that does not match the type
 * instead of reading part of it, which would silently produce a value built
 * from stale bytes.
 */
template <typename T>
inline void read_typed(fastmem_proc *proc, uintptr_t addr, T *out) {
    static_assert(std::is_trivially_copyable<T>::value,
                  "fastmem::read<T> requires a trivially copyable type");
    if (!fastmem_read(proc, addr, out, sizeof(T)))
        fail("read");
}

}  // namespace detail

/* ------------------------------------------------------------------ */
/* Region helpers                                                      */
/* ------------------------------------------------------------------ */

/* A region in value form, so callers need not keep the callback struct. */
struct Region {
    uintptr_t base = 0;
    std::size_t size = 0;
    unsigned state = 0;
    unsigned protect = 0;
    unsigned type = 0;

    uintptr_t end() const noexcept { return base + size; }
    bool committed() const noexcept {
        return (state & FM_STATE_COMMIT) != 0;
    }
    /* Readable means committed and carrying read permission. `protect`
     * holds FM_PROTECT_* permission bits, not the raw PAGE_* value. */
    bool readable() const noexcept {
        return committed() && (protect & FM_PROTECT_READ) != 0;
    }
    bool image() const noexcept { return (type & FM_TYPE_IMAGE) != 0; }
    bool contains(uintptr_t addr) const noexcept {
        return addr >= base && addr < end();
    }
};

/* ------------------------------------------------------------------ */
/* Process                                                             */
/* ------------------------------------------------------------------ */

/*
 * Owns a process handle. Move-only: two owners would double-close.
 */
class Process {
public:
    Process() noexcept = default;

    /* Throws fastmem::error when the process cannot be opened. */
    explicit Process(unsigned pid)
        : proc_(fastmem_open(pid)) {
        if (!proc_)
            detail::fail("could not open process");
    }

    /* Open with explicit rights, e.g. FM_VM_READ | FM_ALL_ACCESS. */
    static Process open_ex(unsigned pid, unsigned access) {
        Process p;
        p.proc_ = fastmem_open_ex(pid, access);
        if (!p.proc_)
            detail::fail("could not open process");
        return p;
    }

    /* Non-throwing form, for probing many PIDs in a loop. */
    static Process try_open(unsigned pid) noexcept {
        Process p;
        p.proc_ = fastmem_open(pid);
        return p;
    }

    ~Process() { fastmem_close(proc_); }

    Process(const Process &) = delete;
    Process &operator=(const Process &) = delete;

    Process(Process &&other) noexcept : proc_(other.proc_) {
        other.proc_ = nullptr;
    }
    Process &operator=(Process &&other) noexcept {
        if (this != &other) {
            fastmem_close(proc_);
            proc_ = other.proc_;
            other.proc_ = nullptr;
        }
        return *this;
    }

    bool valid() const noexcept { return proc_ != nullptr; }
    explicit operator bool() const noexcept { return valid(); }
    unsigned pid() const noexcept { return fastmem_pid(proc_); }
    bool alive() const noexcept { return fastmem_is_alive(proc_) != 0; }

    void close() noexcept {
        fastmem_close(proc_);
        proc_ = nullptr;
    }

    /* Raw handle, for the fastmem_* functions the wrapper does not cover. */
    fastmem_proc *get() const noexcept { return proc_; }

    /* ---------------- single reads ---------------- */

    /* Raw bytes. Throws on failure. */
    std::vector<uint8_t> read_bytes(uintptr_t addr, std::size_t size) const {
        std::vector<uint8_t> buf(size);
        if (size && !fastmem_read(proc_, addr, buf.data(), size))
            detail::fail("read");
        return buf;
    }

    /* Raw bytes, empty on failure. */
    std::vector<uint8_t> read_bytes_or_empty(uintptr_t addr,
                                            std::size_t size) const noexcept {
        std::vector<uint8_t> buf(size);
        if (size && !fastmem_read(proc_, addr, buf.data(), size))
            buf.clear();
        return buf;
    }

    /*
     * Read any trivially copyable type. The size is taken from the type, so
     * there is no way to read 4 bytes into a uint64_t by mistake.
     */
    template <typename T>
    T read(uintptr_t addr) const {
        T value{};
        detail::read_typed(proc_, addr, &value);
        return value;
    }

    /* ---------------- batch reads ---------------- */

    /*
     * Read many addresses into one buffer. Entries that fail are zeroed,
     * matching the into=True behaviour of the Python package; use
     * read_many_masked when the difference between "zero" and "unreadable"
     * matters.
     */
    std::vector<uint8_t> read_many(const std::vector<uintptr_t> &addrs,
                                   std::size_t size = sizeof(uintptr_t))
        const {
        std::vector<uint8_t> out(addrs.size() * size, 0);
        if (!addrs.empty()) {
            fastmem_read_many(proc_, addrs.data(), addrs.size(), size,
                              out.data(), nullptr);
        }
        return out;
    }

    /* Same, but also reports which addresses were readable. */
    void read_many_masked(const std::vector<uintptr_t> &addrs, std::size_t size,
                          std::vector<uint8_t> &out,
                          std::vector<uint8_t> &mask) const {
        out.assign(addrs.size() * size, 0);
        mask.assign(addrs.size(), 0);
        if (!addrs.empty()) {
            fastmem_read_many(proc_, addrs.data(), addrs.size(), size,
                              out.data(), mask.data());
        }
    }

    /*
     * Group nearby addresses so each group costs one ReadProcessMemory.
     * The largest win in the library for dense input.
     */
    std::vector<uint8_t> read_grouped(const std::vector<uintptr_t> &addrs,
                                      std::size_t size, std::size_t span,
                                      std::vector<uint8_t> &mask) const {
        mask.assign(addrs.size(), 0);
        std::vector<uint8_t> out(addrs.size() * size, 0);
        if (!addrs.empty()) {
            fastmem_read_grouped(proc_, addrs.data(), addrs.size(), size,
                                 span, out.data(), mask.data());
        }
        return out;
    }

    /* Same, spread across the worker pool. */
    std::vector<uint8_t> read_many_pooled(const std::vector<uintptr_t> &addrs,
                                           std::size_t size,
                                           unsigned workers,
                                           std::vector<uint8_t> &mask)
        const {
        mask.assign(addrs.size(), 0);
        std::vector<uint8_t> out(addrs.size() * size, 0);
        if (!addrs.empty()) {
            fastmem_pool_read_many(proc_, addrs.data(), addrs.size(), size,
                                   out.data(), mask.data(), workers);
        }
        return out;
    }

    /* ---------------- regions and search ---------------- */

    /* Read a contiguous range. The primitive for scanners. */
    std::vector<uint8_t> read_region(uintptr_t base, std::size_t size) const {
        std::vector<uint8_t> buf(size);
        if (size && !fastmem_read_region(proc_, base, buf.data(), size))
            detail::fail("read_region");
        return buf;
    }

    /*
     * Walk the address space.
     *
     * The three flags are OR-ed bit filters; pass 0 for "any". min_size and
     * max_size of 0 mean unbounded.
     */
    std::vector<Region> regions(unsigned state = 0, unsigned protect = 0,
                                unsigned type = 0, std::size_t min_size = 0,
                                std::size_t max_size = 0) const {
        collector sink;
        fastmem_regions(proc_, state, protect, type, min_size, max_size,
                        &collector::trampoline, &sink);
        return std::move(sink.out);
    }

    /* Committed and readable regions, the usual starting point. */
    std::vector<Region> readable_regions(std::size_t min_size = 0,
                                         std::size_t max_size = 0) const {
        return regions(FM_STATE_COMMIT, FM_PROTECT_READ, 0, min_size,
                       max_size);
    }

    /* Describe the region containing `addr`. Throws when unmapped. */
    Region query(uintptr_t addr) const {
        fastmem_page page{};
        if (!fastmem_query(proc_, addr, &page))
            detail::fail("query");
        return Region{page.region.base, page.region.size, page.region.state,
                      page.region.protect, page.region.type};
    }

    /*
     * Find `needle` in a range. Returns the addresses of every match, up to
     * max_results. An align greater than 1 restricts matches to aligned
     * addresses, which is what pointer hunting wants.
     */
    std::vector<uintptr_t> find(uintptr_t base, std::size_t size,
                                const void *needle, std::size_t needle_len,
                                std::size_t max_results = 64,
                                std::size_t align = 1) const {
        std::vector<uintptr_t> hits(max_results);
        const std::size_t n =
            fastmem_find(proc_, base, size, needle, needle_len, hits.data(),
                         max_results, align);
        hits.resize(n < max_results ? n : max_results);
        return hits;
    }

    /* Convenience overload taking a byte sequence as the needle. */
    template <typename ByteIt>
    std::vector<uintptr_t> find(uintptr_t base, std::size_t size,
                                ByteIt needle_first, ByteIt needle_last,
                                std::size_t max_results = 64,
                                std::size_t align = 1) const {
        const auto *first = reinterpret_cast<const uint8_t *>(&*needle_first);
        const std::size_t len =
            static_cast<std::size_t>(std::distance(needle_first, needle_last));
        return find(base, size, first, len, max_results, align);
    }

    /*
     * Find `needle` across the readable regions.
     *
     * Scanning a whole address space in one call is almost always the wrong
     * thing to do: most of it is unmapped, so the scan spends its time
     * discovering that. Walking the regions first means every byte handed to
     * the searcher is a byte that exists.
     *
     * max_total caps the bytes read. The budget is also spread: a single
     * cap applied per region would let one multi-megabyte region consume
     * all of it and leave the regions after it - which is where the stack
     * and the small heaps live - unscanned.
     */
    std::vector<uintptr_t> find_all(const void *needle, std::size_t needle_len,
                                    std::size_t max_results = 64,
                                    std::size_t align = 1,
                                    std::size_t max_total = 256u << 20,
                                    std::size_t per_region = 8u << 20) const {
        std::vector<uintptr_t> hits;
        std::size_t budget = max_total;

        for (const Region &r : readable_regions()) {
            if (budget == 0 || hits.size() >= max_results)
                break;
            std::size_t span = r.size;
            if (span > per_region)
                span = per_region;
            if (span > budget)
                span = budget;
            budget -= span;
            for (uintptr_t hit : find(r.base, span, needle, needle_len,
                                      max_results - hits.size(), align)) {
                hits.push_back(hit);
                if (hits.size() >= max_results)
                    return hits;
            }
        }
        return hits;
    }

private:
    struct collector {
        std::vector<Region> out;

        static int trampoline(const fastmem_region *r, void *user) {
            auto *self = static_cast<collector *>(user);
            self->out.push_back(Region{r->base, r->size, r->state, r->protect,
                                       r->type});
            return 1;   /* keep going */
        }
    };

    fastmem_proc *proc_ = nullptr;
};

/* ------------------------------------------------------------------ */
/* Free functions                                                      */
/* ------------------------------------------------------------------ */

inline const char *version() noexcept { return fastmem_version(); }

inline std::size_t page_size() noexcept { return fastmem_page_size(); }

inline unsigned pool_workers() noexcept { return fastmem_pool_workers(); }

inline void pool_shutdown() noexcept { fastmem_pool_shutdown(); }

inline std::string strerror(unsigned long code) {
    const char *text = fastmem_strerror(code);
    return text ? std::string(text) : std::string("unknown error");
}

}  // namespace fastmem

#endif  // FASTMEM_HPP