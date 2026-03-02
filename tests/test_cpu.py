"""
CPU / Threading Environment Tests
==================================
Hypotheses to test:
  - Is it 1 core, 2 cores, or 2 cores with 2 threads each (4 logical CPUs)?
  - Does the Python GIL hide true parallelism for threads?
  - Does multiprocessing bypass the GIL and show real multi-core speedup?
  - What is the actual performance gain going from 1→2→4 workers?

Strategy
--------
1. Topology probe  – read /proc/cpuinfo for physical/logical layout
2. Thread test     – CPU-bound work with threading (GIL limited)
3. Process test    – CPU-bound work with multiprocessing (bypasses GIL)
4. speedup matrix  – time the same total work with 1,2,3,4 workers
"""

import math
import multiprocessing
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _cpu_work(n: int) -> int:
    """Pure CPU-bound: sum of square roots up to n."""
    total = 0.0
    for i in range(1, n + 1):
        total += math.sqrt(i)
    return int(total)


def _timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - t0, result


# ---------------------------------------------------------------------------
# 1. Topology
# ---------------------------------------------------------------------------

def test_cpu_topology():
    """Report physical/logical CPU layout."""
    logical = multiprocessing.cpu_count()

    physical_ids = set()
    core_ids = set()
    siblings = None
    cpu_cores = None

    try:
        with open("/proc/cpuinfo") as f:
            current = {}
            for line in f:
                line = line.strip()
                if not line:
                    if current:
                        physical_ids.add(current.get("physical id", "0"))
                        core_ids.add(
                            (current.get("physical id", "0"), current.get("core id", "0"))
                        )
                        if siblings is None:
                            siblings = int(current.get("siblings", 0))
                        if cpu_cores is None:
                            cpu_cores = int(current.get("cpu cores", 0))
                        current = {}
                else:
                    if ":" in line:
                        k, _, v = line.partition(":")
                        current[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    print("\n=== CPU Topology ===")
    print(f"  Logical CPUs (os.cpu_count):  {logical}")
    print(f"  Physical sockets seen:        {len(physical_ids)}")
    print(f"  Unique physical cores seen:   {len(core_ids)}")
    print(f"  Siblings per socket:          {siblings}")
    print(f"  CPU cores per socket:         {cpu_cores}")
    if cpu_cores and siblings:
        threads_per_core = siblings // cpu_cores
        print(f"  Threads per core (SMT):       {threads_per_core}")
        print(f"  VERDICT: {cpu_cores} physical core(s) × {threads_per_core} thread(s) = {logical} logical CPU(s)")
    assert logical >= 1


# ---------------------------------------------------------------------------
# 2. Thread scaling (GIL limited)
# ---------------------------------------------------------------------------

WORK_UNITS = 8          # total chunks of CPU work
# SMT efficiency threshold: real SMT cores share execution units, so 4 logical
# CPUs from 2 physical cores give less than 2× the 2-process speedup.
# 1.3 is a conservative upper bound (i.e. 4 procs < 2-proc speedup × 1.3).
SMT_EFFICIENCY_THRESHOLD = 1.3
WORK_SIZE  = 800_000    # iterations per chunk

def _thread_worker(_):
    return _cpu_work(WORK_SIZE)


def test_thread_scaling():
    """
    Runs the same total CPU work split across 1, 2, 4 threads.
    Due to the Python GIL, threads do NOT run truly in parallel for CPU-bound
    work → expect little-to-no speedup.
    """
    print("\n=== Thread Scaling (CPU-bound, GIL limited) ===")
    results = {}
    for workers in [1, 2, 4]:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            elapsed, _ = _timed(
                lambda w=workers: list(ex.map(_thread_worker, range(WORK_UNITS)))
            )
        results[workers] = elapsed
        speedup = results[1] / elapsed if workers > 1 else 1.0
        print(f"  {workers} thread(s): {elapsed:.3f}s  speedup vs 1={speedup:.2f}x")

    print("  NOTE: speedup near 1.0 confirms GIL limits CPU-bound threads")
    assert results[1] > 0


# ---------------------------------------------------------------------------
# 3. Process scaling (bypasses GIL)
# ---------------------------------------------------------------------------

def test_process_scaling():
    """
    Same total work across 1, 2, 4 processes.
    Bypasses the GIL → should see real speedup proportional to physical cores.
    2 physical cores → ~2x speedup with 2 processes.
    4 logical CPUs (SMT) → marginal extra gain beyond 2 processes.
    """
    print("\n=== Process Scaling (CPU-bound, GIL bypassed) ===")
    results = {}
    for workers in [1, 2, 3, 4]:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            elapsed, _ = _timed(
                lambda w=workers: list(ex.map(_cpu_work, [WORK_SIZE] * WORK_UNITS))
            )
        results[workers] = elapsed
        speedup = results[1] / elapsed if workers > 1 else 1.0
        print(f"  {workers} process(es): {elapsed:.3f}s  speedup vs 1={speedup:.2f}x")

    s2 = results[1] / results[2]
    s4 = results[1] / results[4]
    print(f"\n  Speedup  1→2 processes: {s2:.2f}x")
    print(f"  Speedup  1→4 processes: {s4:.2f}x")
    if s2 >= 1.5:
        print("  VERDICT: Real multi-core parallelism confirmed (≥1.5x with 2 procs)")
    else:
        print("  VERDICT: Little speedup – possible single-core or contention")
    if 1.0 < s4 < s2 * SMT_EFFICIENCY_THRESHOLD:
        print("  SMT HINT: 4 logical < 2× speedup of 2 physical → SMT threads share resources")
    assert results[1] > 0


# ---------------------------------------------------------------------------
# 4. Amdahl efficiency
# ---------------------------------------------------------------------------

def test_parallel_efficiency():
    """
    Compute parallel efficiency = speedup / workers.
    Ideal: 1.0 per worker.  SMT cores typically give 0.6-0.8 efficiency.
    """
    print("\n=== Parallel Efficiency (multiprocessing) ===")
    baseline, _ = _timed(lambda: [_cpu_work(WORK_SIZE)] * WORK_UNITS)
    for workers in [1, 2, 4]:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            elapsed, _ = _timed(
                lambda w=workers: list(ex.map(_cpu_work, [WORK_SIZE] * WORK_UNITS))
            )
        speedup = baseline / elapsed
        efficiency = speedup / workers
        print(f"  {workers:>2} worker(s): {elapsed:.3f}s  speedup={speedup:.2f}x  efficiency={efficiency:.2f}")
    assert True


# ---------------------------------------------------------------------------
# Entry point for direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_cpu_topology()
    test_thread_scaling()
    test_process_scaling()
    test_parallel_efficiency()
    print("\nAll CPU tests completed.")
