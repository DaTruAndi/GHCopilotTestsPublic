"""
RAM Environment Tests
======================
Hypotheses to test:
  - Is there a hard 8 GB limit enforced by the container/hypervisor?
  - How much RAM can we actually allocate before we hit swap?
  - Does the OS start swapping gracefully or do we get killed (OOM)?

Strategy
--------
1. Report current memory stats (total, available, swap)
2. Gradually allocate RAM in 512 MB chunks, document at each step
3. Cross 8 GB threshold and see if anything changes
4. Keep going until swap is used, then document
5. Try to exhaust swap (best-effort – process may be OOM-killed)
"""

import gc
import os
import resource
import sys
import time

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_stats():
    """Return (total_mb, available_mb, swap_total_mb, swap_used_mb)."""
    if _HAS_PSUTIL:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return (
            vm.total / 1024**2,
            vm.available / 1024**2,
            sw.total / 1024**2,
            sw.used / 1024**2,
        )
    # fallback: parse /proc/meminfo
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            parts = v.split()
            info[k.strip()] = int(parts[0]) if parts else 0  # kB
    total     = info.get("MemTotal", 0) / 1024
    available = info.get("MemAvailable", 0) / 1024
    sw_total  = info.get("SwapTotal", 0) / 1024
    sw_used   = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1024
    return total, available, sw_total, sw_used


def _print_mem(label=""):
    total, avail, sw_total, sw_used = _mem_stats()
    tag = f"[{label}] " if label else ""
    print(
        f"  {tag}RAM total={total:.0f} MB  available={avail:.0f} MB  "
        f"swap total={sw_total:.0f} MB  swap used={sw_used:.0f} MB"
    )
    return total, avail, sw_total, sw_used


# ---------------------------------------------------------------------------
# 1. Baseline memory report
# ---------------------------------------------------------------------------

def test_memory_baseline():
    """Measure and report total/available RAM and swap."""
    print("\n=== Memory Baseline ===")
    total, avail, sw_total, sw_used = _print_mem("baseline")
    print(f"  Total system RAM:  {total/1024:.2f} GB")
    print(f"  Available RAM:     {avail/1024:.2f} GB")
    print(f"  Swap total:        {sw_total/1024:.2f} GB")
    print(f"  Swap used:         {sw_used/1024:.2f} GB")

    # Cross-check /proc/meminfo directly
    with open("/proc/meminfo") as f:
        raw = f.read()
    print("\n  /proc/meminfo (selected):")
    for line in raw.splitlines():
        if any(k in line for k in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree")):
            print(f"    {line}")

    assert total > 0


# ---------------------------------------------------------------------------
# 2. Gradual RAM allocation
# ---------------------------------------------------------------------------

CHUNK_MB   = 512        # allocate in 512 MB steps
TARGET_MAX_GB = 14      # try to reach 14 GB total allocated


def _alloc_mb(mb: int) -> bytearray:
    """Allocate and touch (write) mb megabytes so pages are actually used."""
    buf = bytearray(mb * 1024 * 1024)
    # Touch every 4096-byte page to force physical allocation
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    return buf


def test_ram_gradual_fill():
    """
    Gradually fill RAM in 512 MB chunks.
    Document findings before each step, especially around the 8 GB mark.
    Stop when we detect swap usage or reach TARGET_MAX_GB.
    """
    print("\n=== Gradual RAM Fill ===")
    _print_mem("before fill")

    buffers = []
    allocated_mb = 0
    hit_8gb = False
    swap_started = False

    total_mb, _, sw_total_mb, _ = _mem_stats()

    try:
        while allocated_mb < TARGET_MAX_GB * 1024:
            # --- Document BEFORE next allocation ---
            _, avail_mb, _, sw_used_mb = _mem_stats()

            if allocated_mb >= 7 * 1024 and not hit_8gb and allocated_mb < 8 * 1024:
                print(f"\n  >> Approaching 8 GB barrier (allocated={allocated_mb/1024:.1f} GB)")
                _print_mem("pre-8GB")

            # Check if swap started
            if sw_used_mb > 100 and not swap_started:
                swap_started = True
                print(f"\n  *** SWAP STARTED at allocated={allocated_mb/1024:.1f} GB ***")
                _print_mem("swap-start")

            # Stop if almost no RAM left and we are in swap
            if avail_mb < 256 and sw_used_mb > 200:
                print(f"\n  Stopping: very low available RAM ({avail_mb:.0f} MB) + swap active")
                break

            # Allocate next chunk
            actual_chunk = min(CHUNK_MB, TARGET_MAX_GB * 1024 - allocated_mb)
            try:
                buf = _alloc_mb(actual_chunk)
            except MemoryError:
                print(f"\n  MemoryError at allocated={allocated_mb/1024:.1f} GB!")
                break

            buffers.append(buf)
            allocated_mb += actual_chunk

            _, avail_mb_after, _, sw_used_after = _mem_stats()
            print(
                f"  Allocated {allocated_mb/1024:.2f} GB total  "
                f"| RAM avail={avail_mb_after:.0f} MB  swap used={sw_used_after:.0f} MB"
            )

            if not hit_8gb and allocated_mb >= 8 * 1024:
                hit_8gb = True
                print(f"\n  >> Crossed 8 GB allocation – no error, no OOM kill!")
                print(f"     This disproves a hard 8 GB container limit (at the Python level).")
                _print_mem("post-8GB")

    finally:
        print(f"\n  === Final state after fill (allocated {allocated_mb/1024:.2f} GB) ===")
        _print_mem("after fill")
        # Free memory
        del buffers
        gc.collect()
        _print_mem("after free")

    assert allocated_mb > 0


# ---------------------------------------------------------------------------
# 3. Push into swap
# ---------------------------------------------------------------------------

def test_ram_push_into_swap():
    """
    Keep allocating past available RAM to deliberately trigger swap usage.
    Document swap growth.  Stop before OOM kill (leave 256 MB safety margin).
    """
    print("\n=== Push into Swap ===")
    total_mb, avail_mb, sw_total_mb, sw_used_mb = _mem_stats()
    _print_mem("before swap test")

    if sw_total_mb < 1:
        print("  No swap configured – skipping swap push test.")
        return

    # We want to allocate all available RAM + most of swap (leave 512 MB swap free as safety)
    swap_baseline = sw_used_mb  # swap already used before this test
    target_alloc_mb = int(avail_mb + sw_total_mb - 512)
    # Hard cap: never use more than total RAM + 90% of swap
    hard_cap = int(total_mb + sw_total_mb * 0.9)
    target_alloc_mb = min(target_alloc_mb, hard_cap)

    print(f"  Will try to allocate {target_alloc_mb/1024:.2f} GB")
    print(f"  (available RAM = {avail_mb/1024:.2f} GB, swap total = {sw_total_mb/1024:.2f} GB)")
    print(f"  (swap already used = {swap_baseline:.0f} MB)")

    buffers = []
    allocated_mb = 0
    swap_peak = 0.0
    swap_started_here = False

    try:
        while allocated_mb < target_alloc_mb:
            chunk = min(256, target_alloc_mb - allocated_mb)
            try:
                buf = _alloc_mb(chunk)
            except MemoryError:
                print(f"  MemoryError at {allocated_mb/1024:.2f} GB allocated")
                break

            buffers.append(buf)
            allocated_mb += chunk

            _, avail_after, _, sw_used = _mem_stats()
            swap_peak = max(swap_peak, sw_used)

            new_swap = sw_used - swap_baseline
            if not swap_started_here and new_swap > 100:
                swap_started_here = True
                print(f"\n  *** NEW SWAP USAGE STARTED: {new_swap:.0f} MB at {allocated_mb/1024:.2f} GB allocated ***")

            if allocated_mb % 1024 == 0 or new_swap > 100:
                print(
                    f"  {allocated_mb/1024:.2f} GB allocated | "
                    f"avail={avail_after:.0f} MB | swap used={sw_used:.0f} MB (new: {new_swap:.0f} MB)"
                )
            if avail_after < 128:
                print("  Safety stop: available RAM < 128 MB")
                break

    finally:
        _print_mem("peak swap state")
        del buffers
        gc.collect()
        time.sleep(1)
        _print_mem("after swap free")
        print(f"  Peak swap usage observed: {swap_peak:.0f} MB")

    assert True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_memory_baseline()
    test_ram_gradual_fill()
    test_ram_push_into_swap()
    print("\nAll RAM tests completed.")
