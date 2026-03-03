"""
Environment Resource Overview
==============================
Collects live measurements for CPU, RAM, Disk, and OS/Python environment,
then prints a single formatted overview table.

Run standalone:   python3 tests/test_overview.py
Run via pytest:   pytest tests/test_overview.py -v -s
"""

import gc
import math
import multiprocessing
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

# Avoid a circular import – import only the reusable measurement function
try:
    from tests.test_disk import measure_disk_user_space_exhaustion
except ImportError:
    from test_disk import measure_disk_user_space_exhaustion

# ---------------------------------------------------------------------------
# Constants documenting results from the stress-test suite
# (tests/test_ram.py and tests/test_disk.py) – update these if re-measured.
# ---------------------------------------------------------------------------

# RAM: point at which the OS started paging during the gradual-fill stress test
_STRESS_SWAP_TRIGGER = "~13.75 GB allocated (from stress tests)"

# Disk: largest contiguous and aggregate file totals confirmed without error
_STRESS_MAX_SINGLE_FILE = "20 GB (from stress tests)"
_STRESS_MAX_MULTI_FILE  = "20 GB / 205 × 100 MB files (from stress tests)"

# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

def _cpu_work(n: int) -> float:
    """CPU-bound: sum of square roots – used for speedup measurement."""
    total = 0.0
    for i in range(1, n + 1):
        total += math.sqrt(i)
    return total


def _measure_cpu():
    """Return a dict of CPU facts measured at runtime."""
    facts = {}

    # Logical count
    facts["logical_cpus"] = multiprocessing.cpu_count()

    # Read /proc/cpuinfo once and reuse for both topology and hypervisor detection
    cpuinfo_raw = ""
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo_raw = f.read()
    except FileNotFoundError:
        pass

    # Parse topology fields
    physical_ids, core_ids = set(), set()
    siblings = cpu_cores = None
    model_name = cache_size = "n/a"
    current: dict[str, str] = {}
    for line in cpuinfo_raw.splitlines():
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

    facts["physical_cores"] = len(core_ids) if core_ids else "n/a"
    facts["sockets"] = len(physical_ids) if physical_ids else "n/a"
    facts["threads_per_core"] = (
        siblings // cpu_cores if (siblings and cpu_cores) else "n/a"
    )
    facts["model_name"] = model_name
    facts["l2_cache"] = cache_size

    # Hypervisor – reuse the already-read cpuinfo_raw
    try:
        if "hypervisor" in cpuinfo_raw:
            r = subprocess.run(
                ["systemd-detect-virt", "--vm"],
                capture_output=True, text=True, timeout=3
            )
            facts["hypervisor"] = r.stdout.strip() or "yes (vendor unknown)"
        else:
            facts["hypervisor"] = "none (bare metal)"
    except Exception:
        facts["hypervisor"] = "unknown"

    # Measure actual multiprocessing speedup (quick: 4 chunks, 500k iters each)
    n_chunks, work_size = 4, 500_000
    t0 = time.perf_counter()
    [_cpu_work(work_size) for _ in range(n_chunks)]
    t_serial = time.perf_counter() - t0

    speedups = {}
    for w in [1, 2, 4]:
        with ProcessPoolExecutor(max_workers=w) as ex:
            t0 = time.perf_counter()
            list(ex.map(_cpu_work, [work_size] * n_chunks))
            speedups[w] = t_serial / (time.perf_counter() - t0)

    facts["speedup_2proc"] = f"{speedups[2]:.2f}×"
    facts["speedup_4proc"] = f"{speedups[4]:.2f}×"

    return facts


def _measure_ram():
    """Return a dict of RAM facts measured at runtime."""
    facts = {}
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            parts = v.split()
            info[k.strip()] = int(parts[0]) if parts else 0  # kB

    facts["total_gb"]     = f"{info.get('MemTotal', 0) / 1024**2:.2f} GB"
    facts["available_gb"] = f"{info.get('MemAvailable', 0) / 1024**2:.2f} GB"
    facts["used_gb"] = (
        f"{(info.get('MemTotal',0) - info.get('MemAvailable',0)) / 1024**2:.2f} GB"
    )
    facts["swap_total_gb"] = f"{info.get('SwapTotal', 0) / 1024**2:.2f} GB"
    facts["swap_free_gb"]  = f"{info.get('SwapFree',  0) / 1024**2:.2f} GB"
    facts["swap_trigger"]  = _STRESS_SWAP_TRIGGER
    return facts


def _measure_disk():
    """Return a dict of disk facts measured at runtime (includes exhaustion probe)."""
    facts = {}
    st = os.statvfs("/")
    total      = st.f_blocks * st.f_frsize / 1024**3
    free_user  = st.f_bavail * st.f_frsize / 1024**3   # user-available (f_bavail)
    free_root  = st.f_bfree  * st.f_frsize / 1024**3   # root-reserved included
    used       = (st.f_blocks - st.f_bfree) * st.f_frsize / 1024**3
    reserved   = free_root - free_user

    facts["root_total_gb"]    = f"{total:.1f} GB"
    facts["root_used_gb"]     = f"{used:.1f} GB"
    facts["root_free_user_gb"] = f"{free_user:.2f} GB (f_bavail)"
    facts["root_reserved_gb"] = f"{reserved:.2f} GB"
    facts["root_pct_used"]    = f"{used/total*100:.0f}%"

    # Live exhaustion measurement (near-instant via posix_fallocate)
    exhaust = measure_disk_user_space_exhaustion("/")
    facts["exhaust_capacity_gb"]  = f"~{exhaust['capacity_gb']:.2f} GB"
    facts["exhaust_allocated_gb"] = f"{exhaust['allocated_gb']:.2f} GB"
    facts["exhaust_safety_mb"]    = f"{exhaust['safety_margin_mb']} MB"
    facts["exhaust_hit_enospc"]   = "yes" if exhaust["hit_enospc"] else "no (safety stop)"

    facts["max_single_file_written"] = _STRESS_MAX_SINGLE_FILE
    facts["max_multi_file_written"]  = _STRESS_MAX_MULTI_FILE
    return facts


def _measure_os():
    """Return a dict of OS / Python facts."""
    facts = {}
    facts["os_name"]       = platform.system()
    facts["os_version"]    = platform.version()
    facts["os_release"]    = platform.release()
    facts["architecture"]  = platform.machine()
    facts["python_version"] = platform.python_version()
    facts["python_impl"]   = platform.python_implementation()

    # Distro name from /etc/os-release
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    facts["distro"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        facts["distro"] = "n/a"

    return facts


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------

def _render_table(sections: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """Render a list of (section_title, [(key, value)]) into a formatted table."""
    # Determine column widths
    col1_w = max(len(k) for _, rows in sections for k, _ in rows)
    col2_w = max(len(v) for _, rows in sections for _, v in rows)
    col1_w = max(col1_w, 28)
    col2_w = max(col2_w, 40)

    sep   = f"├{'─' * (col1_w + 2)}┼{'─' * (col2_w + 2)}┤"
    top   = f"┌{'─' * (col1_w + 2)}┬{'─' * (col2_w + 2)}┐"
    bot   = f"└{'─' * (col1_w + 2)}┴{'─' * (col2_w + 2)}┘"
    hdr_l = f"┝{'═' * (col1_w + 2)}┿{'═' * (col2_w + 2)}┥"

    lines = [top]
    title_text = "Environment Resource Overview"
    # Title spans both columns: inner width = col1 + " │ " separator + col2 = col1_w + col2_w + 3
    title_w = col1_w + col2_w + 3
    lines.append(f"│ {title_text:^{title_w}} │")
    lines.append(f"├{'─' * (col1_w + 2)}┬{'─' * (col2_w + 2)}┤")
    lines.append(f"│ {'Resource':<{col1_w}} │ {'Measured Value':<{col2_w}} │")
    lines.append(hdr_l)

    for section_idx, (section_title, rows) in enumerate(sections):
        # Section header
        lines.append(f"│ {('▸ ' + section_title).upper():<{col1_w}} │ {'':<{col2_w}} │")
        lines.append(sep)
        for i, (key, value) in enumerate(rows):
            lines.append(f"│ {'  ' + key:<{col1_w}} │ {value:<{col2_w}} │")
            if i < len(rows) - 1:
                lines.append(sep)
        if section_idx < len(sections) - 1:
            lines.append(hdr_l)

    lines.append(bot)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main measurement + table build
# ---------------------------------------------------------------------------

def _build_overview() -> str:
    """Run all measurements and return the formatted table string."""
    cpu  = _measure_cpu()
    ram  = _measure_ram()
    disk = _measure_disk()
    opsys = _measure_os()

    sections = [
        ("Platform", [
            ("OS",             opsys["distro"]),
            ("Kernel",         opsys["os_release"]),
            ("Architecture",   opsys["architecture"]),
            ("Hypervisor",     cpu["hypervisor"]),
            ("Python",         f"{opsys['python_impl']} {opsys['python_version']}"),
        ]),
        ("CPU", [
            ("Model",                  cpu["model_name"]),
            ("Physical cores",         str(cpu["physical_cores"])),
            ("Threads per core (SMT)", str(cpu["threads_per_core"])),
            ("Logical CPUs",           str(cpu["logical_cpus"])),
            ("L2 cache (per core)",    cpu["l2_cache"]),
            ("Parallel speedup 2 proc",  cpu["speedup_2proc"]),
            ("Parallel speedup 4 proc",  cpu["speedup_4proc"]),
            ("GIL (thread speedup)",   "~1.0× (GIL serialises CPU-bound threads)"),
        ]),
        ("RAM", [
            ("Total",           ram["total_gb"]),
            ("Available now",   ram["available_gb"]),
            ("Used now",        ram["used_gb"]),
            ("Swap total",      ram["swap_total_gb"]),
            ("Swap free now",   ram["swap_free_gb"]),
            ("Swap trigger",    ram["swap_trigger"]),
            ("8 GB limit?",     "No — 14 GB+ allocated without error"),
        ]),
        ("Disk (/)", [
            ("Total",                      disk["root_total_gb"]),
            ("Used",                       disk["root_used_gb"]),
            ("User-available (f_bavail)",  disk["root_free_user_gb"]),
            ("Reserved for root",          disk["root_reserved_gb"]),
            ("Used %",                     disk["root_pct_used"]),
            ("User-writable capacity",     disk["exhaust_capacity_gb"]),
            ("Allocated at exhaust stop",  disk["exhaust_allocated_gb"]),
            ("Exhaustion safety margin",   disk["exhaust_safety_mb"]),
            ("ENOSPC raised",              disk["exhaust_hit_enospc"]),
            ("Max single file written",    disk["max_single_file_written"]),
            ("Max multi-file written",     disk["max_multi_file_written"]),
            ("14 GB limit?",               "No — 20 GB written without error"),
        ]),
    ]

    return _render_table(sections)


# ---------------------------------------------------------------------------
# pytest entry point
# ---------------------------------------------------------------------------

def test_overview():
    """Print the resource overview table and assert it was generated."""
    table = _build_overview()
    print("\n")
    print(table)
    assert "Environment Resource Overview" in table
    assert "CPU" in table
    assert "RAM" in table
    assert "DISK" in table


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(_build_overview())
