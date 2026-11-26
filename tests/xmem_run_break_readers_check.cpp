// Readers that do not fault, inside a run break of the shared-view mode (X2W_XMEM_SHARED_VIEWS=1). Unmapping one
// page of a run unmaps the whole run, then maps its other pages again (src/xmem_mapped_pages.cpp, map_pages_locked).
// A thread that touches one of those pages in between faults and is retried; a region query or a kernel copy does
// not fault, so it reads the pages as reserved or fails. Each case below opens that window with the test-controls
// build's X2W_XMEM_TEST_HOLD_WINDOW, proves it is open with a raw VirtualQuery of a neighbour page (not committed),
// starts the reader under test on another thread, checks that it has not answered 300 ms later, releases the window
// and then checks the reader's answer: the neighbour page is committed, mapped and holds its data.
//
// Cases (one window each): x2w_xmem_stable_call, XMemVirtualQuery, this program's VirtualQueryEx import (wrapped by
// xmem.dll when it loads with the switch), x2w_local::writable, x2w_startup::measure_placed_buffer and
// x2w_startup::snapshot_samplers.
//
// With `control` (the build whose local_log.h asks xmem.dll for a missing export, so the header readers run
// directly) the three header readers answer inside the window: the check expects that, and fails if they wait.
//
// Then a page that faults in two breaks in a row (retry_remapped_page): a thread faults on a page of a run taken
// apart by break A, and the test build's X2W_XMEM_TEST_HOLD_RETRY holds the handler until break A has ended, so the
// handler reads the generation after it; the thread is retried and reads its page. Break B then takes the same page
// apart, and the thread faults on it again: that fault must be retried as well (a different break). With
// `next-break-control` the handler is not held, so it reads break A while A is open: that order passes even when
// the handler names the breaks wrongly, which shows the hold is what this case depends on.
//
// Usage: xmem_run_break_readers_check <xmem.dll built with its test controls> [control|next-break-control]
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <d3d12.h>

#include "startup_stubs/local_log.h"
#include "startup_stubs/placed_buffer_range.h"
#include "startup_stubs/translated_sampler_bindings.h"

#include <atomic>
#include <cstdio>
#include <cstring>
#include <functional>
#include <thread>

namespace {
using XMemVirtualAllocFn = void*(WINAPI*)(void*, SIZE_T, DWORD, ULONGLONG, DWORD);
using XMemAllocatePhysicalPagesFn = BOOL(WINAPI*)(SIZE_T, SIZE_T*, SIZE_T*);
using XMemMapPhysicalPagesFn = void*(WINAPI*)(void*, SIZE_T, SIZE_T*);
using XMemFreePhysicalPagesFn = BOOL(WINAPI*)(SIZE_T, SIZE_T*);
using XMemVirtualQueryFn = BOOL(WINAPI*)(HANDLE, LPCVOID, void*);
using StableCallFn = BOOL(WINAPI*)(void (*)(void*), void*);

constexpr SIZE_T page = SIZE_T{64} << 10;
constexpr SIZE_T pages = 8;
constexpr SIZE_T broken_page = 5;   // the page unmapped; its run [0, 8) is taken apart
constexpr SIZE_T neighbour = 2;     // a surviving page of that run, unmapped while the window is held

int failures = 0;

void check(bool ok, const char* name, const char* reader)
{
    std::printf("%s: %s %s\n", ok ? "ok" : "check_failed", reader, name);
    if (!ok) ++failures;
}

DWORD raw_state(const void* address)
{
    MEMORY_BASIC_INFORMATION info{};
    return VirtualQuery(address, &info, sizeof(info)) ? info.State : 0;
}

struct Shim {
    XMemVirtualAllocFn virtual_alloc;
    XMemAllocatePhysicalPagesFn allocate;
    XMemMapPhysicalPagesFn map;
    XMemFreePhysicalPagesFn release;
    XMemVirtualQueryFn query;
    StableCallFn stable_call;
};

// One window: maps a fresh run of eight pages at base, marks every page, unmaps broken_page on a thread of its own
// with the hold set, and runs reader on another thread once the window is open. reader returns whether its answer
// says the neighbour page is committed and readable. waits: the reader must not answer before the release.
void window_case(const Shim& shim, unsigned char* base, int index, const char* name, bool waits,
                 const std::function<bool(unsigned char*)>& reader)
{
    SIZE_T count = pages, tokens[pages] = {};
    if (!shim.allocate(0x20400000, &count, tokens) || count != pages || shim.map(base, pages, tokens) != base) {
        check(false, "map_run", name);
        return;
    }
    for (SIZE_T i = 0; i < pages; ++i) *reinterpret_cast<std::uint32_t*>(base + i * page) = 0x51000000u + static_cast<std::uint32_t>(i);
    char hold[64], event_name[96];
    std::snprintf(hold, sizeof(hold), "x2w-run-break-readers-%lu-%d", GetCurrentProcessId(), index);
    std::snprintf(event_name, sizeof(event_name), "%s.unmapped", hold);
    HANDLE unmapped = CreateEventA(nullptr, TRUE, FALSE, event_name);
    std::snprintf(event_name, sizeof(event_name), "%s.release", hold);
    HANDLE release = CreateEventA(nullptr, TRUE, FALSE, event_name);
    SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_WINDOW", hold);
    unsigned char* const broken = base + broken_page * page;
    std::thread unmapper([&] { shim.map(broken, 1, nullptr); });
    const bool opened = WaitForSingleObject(unmapped, 10000) == WAIT_OBJECT_0;
    check(opened, "window_reached", name);
    unsigned char* const target = base + neighbour * page;
    if (opened) check(raw_state(target) != MEM_COMMIT, "window_open_raw_query_not_committed", name);
    std::atomic<bool> answered{false};
    bool answer = false;
    std::thread reading([&] {
        answer = reader(target);
        answered = true;
    });
    Sleep(300);
    const bool early = answered.load();
    check(waits ? !early : early, waits ? "reader_waits_for_the_break" : "reader_answers_inside_the_window", name);
    SetEvent(release);
    reading.join();
    unmapper.join();
    SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_WINDOW", nullptr);
    check(waits ? answer : !answer, waits ? "answer_after_the_break_committed" : "answer_inside_the_window_not_committed", name);
    check(*reinterpret_cast<std::uint32_t*>(target) == 0x51000000u + neighbour, "neighbour_data_kept", name);
    shim.map(base, broken_page, nullptr);
    shim.map(base + (broken_page + 1) * page, pages - broken_page - 1, nullptr);
    shim.release(pages, tokens);
    CloseHandle(unmapped);
    CloseHandle(release);
}
struct Events {
    char name[64];
    HANDLE first, second;
    Events(const char* stem, const char* a, const char* b)
    {
        std::snprintf(name, sizeof(name), "x2w-run-break-%s-%lu", stem, GetCurrentProcessId());
        char event_name[96];
        std::snprintf(event_name, sizeof(event_name), "%s.%s", name, a);
        first = CreateEventA(nullptr, TRUE, FALSE, event_name);
        std::snprintf(event_name, sizeof(event_name), "%s.%s", name, b);
        second = CreateEventA(nullptr, TRUE, FALSE, event_name);
    }
    ~Events()
    {
        CloseHandle(first);
        CloseHandle(second);
    }
};

// Break A: unmap page 7 of a run of eight; break B: unmap page 6 of what is left, [0, 7). The reader's page 2 is in
// both. hold_retry: the handler for the fault in break A reads the break only after A has ended.
void next_break_case(const Shim& shim, unsigned char* base, bool hold_retry)
{
    const char* const name = hold_retry ? "next_break" : "next_break_control";
    SIZE_T count = pages, tokens[pages] = {};
    if (!shim.allocate(0x20400000, &count, tokens) || count != pages || shim.map(base, pages, tokens) != base) {
        check(false, "map_run", name);
        return;
    }
    for (SIZE_T i = 0; i < pages; ++i) *reinterpret_cast<std::uint32_t*>(base + i * page) = 0x52000000u + static_cast<std::uint32_t>(i);
    unsigned char* const target = base + 2 * page;
    Events window_a("a", "unmapped", "release"), window_b("b", "unmapped", "release"), retry("retry", "faulted", "go");
    HANDLE second_read = CreateEventA(nullptr, TRUE, FALSE, nullptr);
    std::atomic<int> reads{0};
    std::uint32_t first_value = 0, second_value = 0;

    SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_WINDOW", window_a.name);
    std::thread unmap_a([&] { shim.map(base + 7 * page, 1, nullptr); });
    const bool a_open = WaitForSingleObject(window_a.first, 10000) == WAIT_OBJECT_0;
    check(a_open, "break_a_held", name);
    if (hold_retry) SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_RETRY", retry.name);
    std::thread reader([&] {
        first_value = *reinterpret_cast<volatile std::uint32_t*>(target);  // faults in break A
        ++reads;
        WaitForSingleObject(second_read, 10000);
        second_value = *reinterpret_cast<volatile std::uint32_t*>(target);  // faults in break B
        ++reads;
    });
    if (hold_retry) {
        check(WaitForSingleObject(retry.first, 10000) == WAIT_OBJECT_0, "handler_held_before_reading_the_break", name);
        SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_RETRY", nullptr);
    } else {
        Sleep(100);  // the handler reads break A while it is open, then waits for it
    }
    SetEvent(window_a.second);
    unmap_a.join();
    check(raw_state(target) == MEM_COMMIT, "break_a_ended", name);
    if (hold_retry) SetEvent(retry.second);  // the handler now reads the generation after break A
    for (int i = 0; i < 200 && reads.load() < 1; ++i) Sleep(10);
    check(reads.load() == 1 && first_value == 0x52000002u, "first_fault_retried", name);

    SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_WINDOW", window_b.name);
    std::thread unmap_b([&] { shim.map(base + 6 * page, 1, nullptr); });
    const bool b_open = WaitForSingleObject(window_b.first, 10000) == WAIT_OBJECT_0;
    check(b_open && raw_state(target) != MEM_COMMIT, "break_b_held_page_not_committed", name);
    SetEvent(second_read);
    Sleep(300);
    check(reads.load() == 1, "second_fault_waits_for_break_b", name);
    SetEvent(window_b.second);
    unmap_b.join();
    reader.join();
    SetEnvironmentVariableA("X2W_XMEM_TEST_HOLD_WINDOW", nullptr);
    check(reads.load() == 2 && second_value == 0x52000002u, "second_fault_retried", name);
    shim.map(base, 6, nullptr);
    shim.release(pages, tokens);
    CloseHandle(second_read);
    if (!hold_retry) std::printf("next-break control: the handler read break A while it was open\n");
}
}  // namespace

int main(int argc, char** argv)
{
    const bool control = argc > 2 && std::strcmp(argv[2], "control") == 0;
    const bool next_break_control = argc > 2 && std::strcmp(argv[2], "next-break-control") == 0;
    if (argc < 2 || (argc > 2 && !control && !next_break_control)) {
        std::printf("usage: xmem_run_break_readers_check <test-controls xmem.dll> [control|next-break-control]\n");
        return 2;
    }
    // The mode is read by the first mapping call; the VirtualQueryEx import is wrapped when xmem.dll loads.
    SetEnvironmentVariableA("X2W_XMEM_SHARED_VIEWS", "1");
    const HMODULE xmem = LoadLibraryA(argv[1]);
    if (!xmem) {
        std::printf("check_failed: load (error %lu)\n", GetLastError());
        return 1;
    }
    Shim shim{reinterpret_cast<XMemVirtualAllocFn>(GetProcAddress(xmem, "XMemVirtualAlloc")),
              reinterpret_cast<XMemAllocatePhysicalPagesFn>(GetProcAddress(xmem, "XMemAllocatePhysicalPages")),
              reinterpret_cast<XMemMapPhysicalPagesFn>(GetProcAddress(xmem, "XMemMapPhysicalPages")),
              reinterpret_cast<XMemFreePhysicalPagesFn>(GetProcAddress(xmem, "XMemFreePhysicalPages")),
              reinterpret_cast<XMemVirtualQueryFn>(GetProcAddress(xmem, "XMemVirtualQuery")),
              reinterpret_cast<StableCallFn>(GetProcAddress(xmem, "x2w_xmem_stable_call"))};
    if (!shim.virtual_alloc || !shim.allocate || !shim.map || !shim.release || !shim.query || !shim.stable_call) {
        std::printf("check_failed: exports\n");
        return 1;
    }
    auto* const base = static_cast<unsigned char*>(shim.virtual_alloc(nullptr, SIZE_T{16} << 30, 0x20402000, 0x21, PAGE_READWRITE));
    if (!base) {
        std::printf("check_failed: startup reservation (error %lu)\n", GetLastError());
        return 1;
    }
    unsigned char* const at = base + page;
    if (next_break_control) {
        next_break_case(shim, at, false);
        VirtualFree(base, 0, MEM_RELEASE);
        std::printf(failures ? "FAIL (%d)\n" : "PASS\n", failures);
        return failures ? 1 : 0;
    }
    int index = 0;
    if (!control) {
        window_case(shim, at, index++, "x2w_xmem_stable_call", true, [&](unsigned char* target) {
            struct Query { unsigned char* target; DWORD state; } query{target, 0};
            shim.stable_call([](void* context) {
                auto& q = *static_cast<Query*>(context);
                q.state = raw_state(q.target);
            }, &query);
            return query.state == MEM_COMMIT;
        });
        window_case(shim, at, index++, "XMemVirtualQuery", true, [&](unsigned char* target) {
            unsigned char buffer[64] = {};
            DWORD state = 0;
            if (shim.query(GetCurrentProcess(), target, buffer)) std::memcpy(&state, buffer + 0x20, sizeof(state));
            return state == MEM_COMMIT;
        });
        window_case(shim, at, index++, "VirtualQueryEx_import", true, [&](unsigned char* target) {
            MEMORY_BASIC_INFORMATION info{};
            return VirtualQueryEx(GetCurrentProcess(), target, &info, sizeof(info)) == sizeof(info) && info.State == MEM_COMMIT;
        });
    }
    // The header readers: waiting with the export, answering inside the window without it (control).
    const bool waits = !control;
    window_case(shim, at, index++, "x2w_local::writable", waits, [&](unsigned char* target) {
        return x2w_local::writable(target, 64);
    });
    window_case(shim, at, index++, "measure_placed_buffer", waits, [&](unsigned char* target) {
        x2w_startup::PlacedBufferRange range;
        return x2w_startup::measure_placed_buffer(target, 4096, range) && range.type == MEM_MAPPED && range.covered;
    });
    window_case(shim, at, index++, "snapshot_samplers", waits, [&](unsigned char* target) {
        const auto snapshot = x2w_startup::snapshot_samplers({reinterpret_cast<std::uintptr_t>(target), 4}, false);
        return snapshot.count == 4 && snapshot.readable.count() == 4 && snapshot.words[0][0] == 0x51000000u + neighbour;
    });
    if (!control) next_break_case(shim, at, true);
    VirtualFree(base, 0, MEM_RELEASE);
    std::printf(failures ? "FAIL (%d)\n" : "PASS\n", failures);
    return failures ? 1 : 0;
}
