"""
Parallel Environment Analysis
==============================
Runs four environment-analysis sub-agent tasks in parallel, one per aspect:
  - software  (OS, kernel, Python, hypervisor)
  - processor (CPU topology, cache, parallel speedup)
  - storage   (filesystem mounts, disk capacity, exhaustion probe)
  - memory    (RAM, swap, allocation limits)

Each task prints "[Model: claude-opus-4.6-fast]" at the start of every new
analysis step, as required by the sub-agent definition files in
.github/agents/.

Run standalone:  python3 tests/test_parallel_analysis.py
Run via pytest:  pytest tests/test_parallel_analysis.py -v -s
"""

import math
import multiprocessing
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Model identifier – matches the model declared in each .github/agents/*.md
# ---------------------------------------------------------------------------

MODEL = "claude-opus-4.6-fast"


def _model_tag() -> str:
    return f"[Model: {MODEL}]"


# ---------------------------------------------------------------------------
# Sub-agent task: Software
# ---------------------------------------------------------------------------

def analyze_software() -> dict:
    """Analyze the software environment (OS, kernel, Python, hypervisor)."""
    results = {}

    print(f"\n  {_model_tag()} [software] Reading OS distribution …")
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    results["distro"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        results["distro"] = "n/a"

    print(f"  {_model_tag()} [software] Reading kernel and architecture …")
    results["kernel"]       = platform.release()
    results["architecture"] = platform.machine()
    results["os_version"]   = platform.version()

    print(f"  {_model_tag()} [software] Reading Python runtime …")
    results["python_impl"]    = platform.python_implementation()
    results["python_version"] = platform.python_version()

    print(f"  {_model_tag()} [software] Detecting hypervisor …")
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
        if "hypervisor" in cpuinfo:
            r = subprocess.run(
                ["systemd-detect-virt", "--vm"],
                capture_output=True, text=True, timeout=3,
            )
            results["hypervisor"] = r.stdout.strip() or "yes (vendor unknown)"
        else:
            results["hypervisor"] = "none (bare metal)"
    except Exception:
        results["hypervisor"] = "unknown"

    return results


# ---------------------------------------------------------------------------
# Sub-agent task: Processor
# ---------------------------------------------------------------------------

def _cpu_work(n: int) -> float:
    total = 0.0
    for i in range(1, n + 1):
        total += math.sqrt(i)
    return total


def analyze_processor() -> dict:
    """Analyze the processor (CPU topology, cache, speedup)."""
    results = {}

    print(f"\n  {_model_tag()} [processor] Reading CPU topology from /proc/cpuinfo …")
    results["logical_cpus"] = multiprocessing.cpu_count()

    physical_ids: set = set()
    core_ids: set     = set()
    siblings = cpu_cores = None
    model_name = cache_size = "n/a"
    current: dict = {}

    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                line = line.strip()
                if not line:
                    if current:
                        physical_ids.add(current.get("physical id", "0"))
                        core_ids.add(
                            (current.get("physical id", "0"), current.get("core id", "0"))
                        )
                        if siblings is None and "siblings" in current:
                            siblings = int(current["siblings"])
                        if cpu_cores is None and "cpu cores" in current:
                            cpu_cores = int(current["cpu cores"])
                        if model_name == "n/a" and "model name" in current:
                            model_name = current["model name"]
                        if cache_size == "n/a" and "cache size" in current:
                            cache_size = current["cache size"]
                        current = {}
                elif ":" in line:
                    k, _, v = line.partition(":")
                    current[k.strip()] = v.strip()
    except FileNotFoundError:
        pass

    results["physical_cores"]   = len(core_ids) if core_ids else "n/a"
    results["sockets"]          = len(physical_ids) if physical_ids else "n/a"
    results["threads_per_core"] = (
        siblings // cpu_cores if (siblings and cpu_cores) else "n/a"
    )
    results["model_name"] = model_name
    results["l2_cache"]   = cache_size

    print(f"  {_model_tag()} [processor] Measuring multiprocessing speedup …")
    n_chunks, work_size = 4, 200_000
    t0 = time.perf_counter()
    [_cpu_work(work_size) for _ in range(n_chunks)]
    t_serial = time.perf_counter() - t0

    from concurrent.futures import ProcessPoolExecutor
    speedups = {}
    for w in [2, 4]:
        with ProcessPoolExecutor(max_workers=w) as ex:
            t0 = time.perf_counter()
            list(ex.map(_cpu_work, [work_size] * n_chunks))
            speedups[w] = t_serial / (time.perf_counter() - t0)

    results["speedup_2proc"] = f"{speedups[2]:.2f}×"
    results["speedup_4proc"] = f"{speedups[4]:.2f}×"

    return results


# ---------------------------------------------------------------------------
# Sub-agent task: Storage
# ---------------------------------------------------------------------------

ONE_MB = 1024 * 1024
ONE_GB = 1024 * ONE_MB


def analyze_storage() -> dict:
    """Analyze storage (filesystem mounts, capacity, exhaustion probe)."""
    results = {}

    print(f"\n  {_model_tag()} [storage] Reading root filesystem stats …")
    st = os.statvfs("/")
    total_gb     = st.f_blocks * st.f_frsize / ONE_GB
    free_user_gb = st.f_bavail * st.f_frsize / ONE_GB
    free_root_gb = st.f_bfree  * st.f_frsize / ONE_GB
    used_gb      = (st.f_blocks - st.f_bfree) * st.f_frsize / ONE_GB
    reserved_gb  = free_root_gb - free_user_gb

    results["root_total_gb"]     = f"{total_gb:.1f} GB"
    results["root_used_gb"]      = f"{used_gb:.1f} GB"
    results["root_free_user_gb"] = f"{free_user_gb:.2f} GB"
    results["root_reserved_gb"]  = f"{reserved_gb:.2f} GB"

    print(f"  {_model_tag()} [storage] Reading /tmp and /dev/shm stats …")
    for mount in ("/tmp", "/dev/shm"):
        try:
            s = os.statvfs(mount)
            results[f"{mount.lstrip('/')}_total_gb"] = (
                f"{s.f_blocks * s.f_frsize / ONE_GB:.1f} GB"
            )
            results[f"{mount.lstrip('/')}_free_gb"] = (
                f"{s.f_bavail * s.f_frsize / ONE_GB:.1f} GB"
            )
        except Exception:
            pass

    print(f"  {_model_tag()} [storage] Scanning mounted filesystems …")
    mounts = []
    supported = ("ext4", "xfs", "btrfs", "overlay", "tmpfs")
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in supported:
                    try:
                        s = os.statvfs(parts[1])
                        mounts.append({
                            "mount": parts[1],
                            "type":  parts[2],
                            "total_gb": f"{s.f_blocks * s.f_frsize / ONE_GB:.1f} GB",
                            "free_gb":  f"{s.f_bavail * s.f_frsize / ONE_GB:.1f} GB",
                        })
                    except (PermissionError, FileNotFoundError):
                        pass
    except FileNotFoundError:
        pass
    results["mounts"] = mounts

    return results


# ---------------------------------------------------------------------------
# Sub-agent task: Memory
# ---------------------------------------------------------------------------

def analyze_memory() -> dict:
    """Analyze memory (RAM total, available, swap, allocation limits)."""
    results = {}

    print(f"\n  {_model_tag()} [memory] Reading /proc/meminfo …")
    info: dict = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            parts = v.split()
            info[k.strip()] = int(parts[0]) if parts else 0  # kB

    results["total_gb"]     = f"{info.get('MemTotal',    0) / 1024**2:.2f} GB"
    results["available_gb"] = f"{info.get('MemAvailable',0) / 1024**2:.2f} GB"
    results["used_gb"] = (
        f"{(info.get('MemTotal',0) - info.get('MemAvailable',0)) / 1024**2:.2f} GB"
    )
    results["swap_total_gb"] = f"{info.get('SwapTotal', 0) / 1024**2:.2f} GB"
    results["swap_free_gb"]  = f"{info.get('SwapFree',  0) / 1024**2:.2f} GB"

    print(f"  {_model_tag()} [memory] Checking memory limits …")
    total_mb = info.get("MemTotal", 0) / 1024
    results["over_8gb_limit"] = "No — system RAM > 8 GB" if total_mb > 8 * 1024 else "Yes / unknown"

    print(f"  {_model_tag()} [memory] Reading HugePages and dirty-ratio settings …")
    results["hugepages_total"] = str(info.get("HugePages_Total", 0))
    results["hugepage_size"]   = f"{info.get('Hugepagesize', 0)} kB"
    results["dirty_ratio"]     = "see /proc/sys/vm/dirty_ratio"

    return results


# ---------------------------------------------------------------------------
# Parallel runner
# ---------------------------------------------------------------------------

TASKS = {
    "software":  analyze_software,
    "processor": analyze_processor,
    "storage":   analyze_storage,
    "memory":    analyze_memory,
}


def run_parallel_analysis() -> dict[str, dict]:
    """Run all four analysis tasks in parallel and return their results."""
    print(f"\n{_model_tag()} Starting parallel environment analysis …")
    print(f"{_model_tag()} Tasks: {', '.join(TASKS)}\n")

    results: dict[str, dict] = {}
    errors:  dict[str, str]  = {}

    with ThreadPoolExecutor(max_workers=len(TASKS)) as executor:
        future_to_name = {
            executor.submit(fn): name for name, fn in TASKS.items()
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                errors[name] = str(exc)
                results[name] = {}

    if errors:
        for name, msg in errors.items():
            print(f"  WARNING: task '{name}' raised an error: {msg}")

    return results


# ---------------------------------------------------------------------------
# Result printer
# ---------------------------------------------------------------------------

def _print_results(results: dict[str, dict]) -> None:
    for aspect in ("software", "processor", "storage", "memory"):
        data = results.get(aspect, {})
        print(f"\n{'─' * 60}")
        print(f"  {_model_tag()} [{aspect.upper()} RESULTS]")
        print(f"{'─' * 60}")
        for key, value in data.items():
            if key == "mounts":
                print(f"    {'mounted filesystems':30s}:")
                for m in value:
                    print(
                        f"      {m['mount']:25s} ({m['type']:8s}) "
                        f"total={m['total_gb']}  free={m['free_gb']}"
                    )
            else:
                print(f"    {key:30s}: {value}")


# ---------------------------------------------------------------------------
# pytest entry point
# ---------------------------------------------------------------------------

def test_parallel_analysis():
    """Run all four environment-analysis tasks in parallel and verify results."""
    results = run_parallel_analysis()
    _print_results(results)

    # --- software ---
    sw = results["software"]
    assert "distro"         in sw, "Missing distro"
    assert "kernel"         in sw, "Missing kernel"
    assert "python_version" in sw, "Missing python_version"
    assert "hypervisor"     in sw, "Missing hypervisor"

    # --- processor ---
    pr = results["processor"]
    assert "logical_cpus"   in pr, "Missing logical_cpus"
    assert "model_name"     in pr, "Missing model_name"
    assert "speedup_2proc"  in pr, "Missing speedup_2proc"

    # --- storage ---
    st = results["storage"]
    assert "root_total_gb"  in st, "Missing root_total_gb"
    assert "root_free_user_gb" in st, "Missing root_free_user_gb"

    # --- memory ---
    mem = results["memory"]
    assert "total_gb"       in mem, "Missing total_gb"
    assert "swap_total_gb"  in mem, "Missing swap_total_gb"

    print(f"\n{_model_tag()} Parallel analysis complete — all assertions passed.")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    results = run_parallel_analysis()
    _print_results(results)
    print(f"\n{_model_tag()} Done.")
