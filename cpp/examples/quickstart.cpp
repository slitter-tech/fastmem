/*
 * quickstart.cpp - exercises the C++ wrapper against the current process.
 *
 * Reading your own process needs no privileges, so this doubles as a smoke
 * test that the header compiles and the calls work. Build:
 *
 *     python tools/build_native.py --example
 *     build\native\quickstart.exe
 */

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <windows.h>

#include <fastmem/fastmem.hpp>

namespace {

void banner(const char *title) {
    std::printf("\n=== %s ===\n", title);
}

/*
 * Store the address of a local in a variable and read it back through the
 * wrapper. Comparing the value against the value is the honest check: the
 * variable holds an address, and reading it must return that address.
 */
void show_pointer_roundtrip(fastmem::Process &p, const void *address) {
    uintptr_t stored = reinterpret_cast<uintptr_t>(address);
    const uintptr_t slot = reinterpret_cast<uintptr_t>(&stored);
    const uintptr_t seen = p.read<uintptr_t>(slot);
    std::printf("stored %p at %p, read back %p  %s\n",
                reinterpret_cast<const void *>(stored),
                reinterpret_cast<const void *>(slot),
                reinterpret_cast<const void *>(seen),
                seen == stored ? "match" : "MISMATCH");
}

void walk_regions(fastmem::Process &p) {
    const auto regions = p.readable_regions();
    std::size_t total = 0;
    for (const auto &r : regions)
        total += r.size;
    std::printf("readable regions: %zu, %.1f MiB total\n", regions.size(),
                static_cast<double>(total) / (1024.0 * 1024.0));

    /* Regions near 1 MiB or larger: the interesting ones for a scanner. */
    const auto big = p.regions(0, 0, 0, 1u << 20);
    std::printf("regions >= 1 MiB:  %zu\n", big.size());
}

void find_own_marker(fastmem::Process &p, uintptr_t marker_addr,
                     const void *needle, std::size_t len) {
    /* Search the region that actually holds the marker first: it is small,
     * known-good, and it makes a failure here unambiguous. */
    const auto where = p.query(marker_addr);
    std::printf("marker at %p lives in region %p..%p (%zu KiB)\n",
                reinterpret_cast<const void *>(marker_addr),
                reinterpret_cast<const void *>(where.base),
                reinterpret_cast<const void *>(where.end()),
                where.size / 1024);

    const auto local = p.find(where.base, where.size, needle, len, 8);
    std::printf("  search that region -> %zu hit(s)", local.size());
    for (uintptr_t hit : local)
        std::printf(" %p", reinterpret_cast<const void *>(hit));
    bool found = false;
    for (uintptr_t hit : local)
        found = found || hit == marker_addr;
    std::printf("%s\n", found ? "  (marker located)" : "  (MARKER MISSING)");

    /* Then the general sweep across every readable region. */
    const auto all = p.readable_regions();
    int covers = 0;
    for (const auto &r : all)
        covers += r.contains(marker_addr) ? 1 : 0;
    std::printf("  readable_regions -> %zu regions, %d cover the marker\n",
                all.size(), covers);
    if (covers == 0) {
        std::printf("  highest region returned: %p..%p (marker is %p)\n",
                    reinterpret_cast<const void *>(all.back().base),
                    reinterpret_cast<const void *>(all.back().end()),
                    reinterpret_cast<const void *>(marker_addr));
    }
    const auto hits = p.find_all(needle, len, 8);
    std::printf("  find_all -> %zu hit(s)\n", hits.size());
}

/*
 * Median-of-runs timing. Returns microseconds per call.
 *
 * `items` is how many addresses one call covers, and is separate from
 * `repeats`: dividing one by the other twice was what made an earlier
 * version of this report 47 us/address where the truth was 0.47.
 */
template <typename Fn>
double timed(const char *label, std::size_t items, std::size_t repeats,
             Fn &&fn) {
    fn();   /* warm up */
    const auto start = std::chrono::steady_clock::now();
    for (std::size_t i = 0; i < repeats; ++i)
        fn();
    const auto elapsed = std::chrono::steady_clock::now() - start;
    const double per_call =
        std::chrono::duration<double, std::micro>(elapsed).count() / repeats;
    std::printf("%-34s %9.1f us/call  %6.3f us/address\n", label, per_call,
                per_call / items);
    return per_call;
}

}  // namespace

/* getenv without the deprecation warning, and without a fixed buffer. */
static std::string env_or_empty(const char *name) {
    char *value = nullptr;
    std::size_t len = 0;
    if (_dupenv_s(&value, &len, name) != 0 || !value)
        return std::string();
    std::string out(value);
    free(value);
    return out;
}

int main() {
    const std::string pid_text = env_or_empty("FASTMEM_DEMO_PID");
    const unsigned pid = pid_text.empty()
                             ? 0u
                             : static_cast<unsigned>(std::atoi(
                                   pid_text.c_str()));
    const unsigned target = pid ? pid : GetCurrentProcessId();

    std::printf("fastmem C++ %s, page size %zu\n", fastmem::version(),
                fastmem::page_size());

    fastmem::Process p;
    try {
        p = fastmem::Process(target);
    } catch (const fastmem::error &e) {
        std::printf("could not open pid %u: %s\n", target, e.what());
        return 1;
    }

    banner("basics");
    std::printf("pid %u, alive: %s\n", p.pid(), p.alive() ? "yes" : "no");

    /* Not volatile: the address is read through another process view, and
     * qualifiers cannot be cast away for the needle pointer. */
    uint64_t marker = 0x1122334455667788ULL;
    show_pointer_roundtrip(p, &marker);
    std::printf("marker value at %p: 0x%llx\n",
                static_cast<const void *>(&marker),
                static_cast<unsigned long long>(
                    p.read<uint64_t>(reinterpret_cast<uintptr_t>(&marker))));

    banner("regions");
    walk_regions(p);

    banner("search");
    find_own_marker(p, reinterpret_cast<uintptr_t>(&marker), &marker, sizeof marker);

    /*
     * Batch timings only mean something against mapped memory. Reading
     * unmapped addresses measures the cost of failing, which is large and
     * wildly variable, and it would swamp the difference between the
     * variants being compared.
     */
    constexpr std::size_t kCount = 2000;
    std::vector<uint8_t> arena(kCount * 4096, 0xAB);
    const uintptr_t arena_base = reinterpret_cast<uintptr_t>(arena.data());

    banner("batched reads, 2000 addresses 64 KiB apart");
    /* Far apart, so grouping has nothing to merge: the case threads exist
     * for. */
    std::vector<uintptr_t> addrs;
    addrs.reserve(kCount);
    for (std::size_t i = 0; i < kCount; ++i)
        addrs.push_back(arena_base + i * 4096);

    std::vector<uint8_t> mask;
    std::vector<uint8_t> sink;
    const double one = timed("read_many, 1 thread", addrs.size(), 50, [&] {
        p.read_many_masked(addrs, 8, sink, mask);
    });
    const double four = timed("read_many_pooled, 4 workers", addrs.size(), 50, [&] {
        p.read_many_pooled(addrs, 8, 4, mask);
    });
    std::printf("thread speedup: %.2fx\n", one / four);

    /* A timing measured against unmapped addresses is a measurement of the
     * cost of failing, so confirm the batch actually read something. */
    std::size_t ok = 0;
    for (uint8_t bit : mask)
        ok += bit ? 1 : 0;
    std::printf("readable addresses: %zu of %zu\n", ok, addrs.size());

    banner("grouping, 2000 addresses 64 bytes apart");
    std::vector<uintptr_t> dense;
    dense.reserve(kCount);
    for (std::size_t i = 0; i < kCount; ++i)
        dense.push_back(arena_base + i * 64);

    const double dense_one = timed("read_many, 1 thread", dense.size(), 50, [&] {
        p.read_many_masked(dense, 8, sink, mask);
    });
    const double dense_grouped = timed("read_grouped, 16 KiB span", dense.size(), 50, [&] {
        p.read_grouped(dense, 8, 16 * 1024, mask);
    });
    std::printf("grouping speedup: %.1fx\n", dense_one / dense_grouped);

    banner("large block");
    const auto block = p.read_region(0, 0);   /* fails, expected */
    std::printf("read_region(0, 0) returned %zu bytes\n", block.size());

    banner("errors");
    try {
        p.read<uintptr_t>(0x1000);
    } catch (const fastmem::error &e) {
        std::printf("read(0x1000) -> code %lu: %s\n", e.code(), e.what());
    }

    fastmem::pool_shutdown();
    std::printf("\nworkers after shutdown: %u\n", fastmem::pool_workers());
    return 0;
}