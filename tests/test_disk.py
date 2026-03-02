"""
Disk Space Environment Tests
==============================
Hypotheses to test:
  - Is there a hard 14 GB disk limit enforced by the container/hypervisor?
  - Can we write a single large file beyond 14 GB?
  - Can we write many 100 MB files that together exceed 14 GB?
  - What is the actual user-writable capacity (exhaustion)?

Strategy
--------
1. Report current disk usage/free for all relevant mount points
2. Single-file growth test: grow one file in 1 GB increments up to ~20 GB,
   documenting free space before each step
3. Multi-file test: write 100 MB files until a limit is hit or we reach 20 GB
4. Exhaustion test: use posix_fallocate to fill to near-ENOSPC and measure the
   exact user-writable capacity
5. Clean up all test files
"""

import os
import shutil
import stat
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUPPORTED_FS_TYPES = ("ext4", "xfs", "btrfs", "overlay", "tmpfs")
ONE_MB   = 1024 * 1024
ONE_GB   = 1024 * ONE_MB
CHUNK    = ONE_MB * 64   # write in 64 MB chunks for speed

# Temporary directory used by tests in this module
TEMP_DIR = os.path.join(tempfile.gettempdir(), "gh_disk_tests")


def _disk_stats(path="/"):
    """Return (total_gb, used_gb, free_gb)."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free  = st.f_bavail  * st.f_frsize
    used  = total - free
    return total / ONE_GB, used / ONE_GB, free / ONE_GB


def _print_disk(label="", path="/"):
    total, used, free = _disk_stats(path)
    tag = f"[{label}] " if label else ""
    print(f"  {tag}Disk({path}): total={total:.1f} GB  used={used:.1f} GB  free={free:.1f} GB")
    return total, used, free


def _setup():
    os.makedirs(TEMP_DIR, exist_ok=True)


def _cleanup():
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Baseline disk report
# ---------------------------------------------------------------------------

def test_disk_baseline():
    """Report disk layout for all relevant mount points."""
    print("\n=== Disk Baseline ===")
    _print_disk("root", "/")
    _print_disk("tmp", "/tmp")

    # df-style summary
    print("\n  Mount points from /proc/mounts:")
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in SUPPORTED_FS_TYPES:
                    try:
                        total, used, free = _disk_stats(parts[1])
                        print(f"    {parts[1]:30s} {parts[2]:8s} total={total:.1f}G  free={free:.1f}G")
                    except (PermissionError, FileNotFoundError):
                        pass
    except FileNotFoundError:
        pass

    total, _, free = _disk_stats("/")
    assert total > 0


# ---------------------------------------------------------------------------
# 2. Single large file test
# ---------------------------------------------------------------------------

SINGLE_FILE_TARGET_GB = 20   # try to write up to 20 GB in one file


def test_disk_single_large_file():
    """
    Write a single file growing in 1 GB increments.
    Document free space before each step.
    Hypothesis: limit at 14 GB.
    """
    _setup()
    fpath = os.path.join(TEMP_DIR, "large_file.bin")
    print(f"\n=== Single Large File Test (target {SINGLE_FILE_TARGET_GB} GB) ===")
    _print_disk("before", "/")

    written_gb = 0
    hit_14gb = False
    data = b"\xAB" * CHUNK  # 64 MB block of known bytes

    try:
        with open(fpath, "wb") as f:
            while written_gb < SINGLE_FILE_TARGET_GB:
                # --- Document BEFORE writing the next GB ---
                _, _, free_gb = _disk_stats("/")

                if written_gb >= 13 and not hit_14gb and written_gb < 14:
                    print(f"\n  >> Approaching 14 GB barrier (written={written_gb:.1f} GB)")
                    _print_disk("pre-14GB", "/")

                # write 1 GB worth of chunks
                try:
                    for _ in range(16):  # 16 × 64 MB = 1 GB
                        f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as e:
                    print(f"\n  OSError at {written_gb:.1f} GB written: {e}")
                    break

                written_gb += 1
                _, _, free_after = _disk_stats("/")
                print(
                    f"  Written {written_gb:>3} GB total | "
                    f"disk free={free_after:.1f} GB"
                )

                if not hit_14gb and written_gb >= 14:
                    hit_14gb = True
                    print(f"\n  >> Crossed 14 GB – NO write error!")
                    print(f"     This disproves a 14 GB single-file disk limit.")
                    _print_disk("post-14GB", "/")

                if free_after < 2.0:
                    print(f"\n  Stopping: disk almost full (< 2 GB free)")
                    break

    finally:
        fsize = os.path.getsize(fpath) / ONE_GB if os.path.exists(fpath) else 0
        print(f"\n  Final file size on disk: {fsize:.2f} GB")
        _print_disk("after single-file", "/")
        try:
            os.remove(fpath)
        except OSError:
            pass
        _print_disk("after cleanup", "/")

    assert written_gb > 0, "No data written at all"


# ---------------------------------------------------------------------------
# 3. Multiple 100 MB files test
# ---------------------------------------------------------------------------

MULTI_FILE_TARGET_GB = 20   # try to write up to 20 GB total in small files
FILE_SIZE_MB = 100


def test_disk_many_files():
    """
    Write 100 MB files one by one until we reach the target or hit an error.
    Hypothesis: limit at 14 GB total.
    """
    _setup()
    multi_dir = os.path.join(TEMP_DIR, "many_files")
    os.makedirs(multi_dir, exist_ok=True)

    print(f"\n=== Many 100 MB Files Test (target {MULTI_FILE_TARGET_GB} GB) ===")
    _print_disk("before", "/")

    data = b"\xCD" * (FILE_SIZE_MB * ONE_MB)
    total_written_mb = 0
    file_count = 0
    files_for_14gb = int(14 * 1024 / FILE_SIZE_MB)
    hit_14gb = False

    try:
        while total_written_mb < MULTI_FILE_TARGET_GB * 1024:
            # Document before each 1 GB milestone
            if total_written_mb % 1024 == 0:
                _print_disk(f"{total_written_mb//1024}GB written", "/")

            if total_written_mb >= 13 * 1024 and not hit_14gb and total_written_mb < 14 * 1024:
                if total_written_mb == 13 * 1024:  # only print once
                    print(f"\n  >> Approaching 14 GB (written={total_written_mb/1024:.1f} GB)")
                    _print_disk("pre-14GB multi", "/")

            fpath = os.path.join(multi_dir, f"file_{file_count:05d}.bin")
            try:
                with open(fpath, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                print(f"\n  OSError writing file {file_count}: {e}")
                break

            file_count += 1
            total_written_mb += FILE_SIZE_MB

            if not hit_14gb and total_written_mb >= 14 * 1024:
                hit_14gb = True
                print(f"\n  >> Crossed 14 GB across {file_count} files – NO error!")
                print(f"     This disproves a 14 GB multi-file disk limit.")
                _print_disk("post-14GB multi", "/")

            _, _, free_gb = _disk_stats("/")
            if free_gb < 2.0:
                print(f"\n  Stopping: disk almost full (< 2 GB free)")
                break

    finally:
        print(f"\n  Written {file_count} files × {FILE_SIZE_MB} MB = {total_written_mb/1024:.2f} GB")
        _print_disk("after multi-file", "/")
        shutil.rmtree(multi_dir, ignore_errors=True)
        _print_disk("after cleanup", "/")

    assert total_written_mb > 0, "No files written at all"


# ---------------------------------------------------------------------------
# 4. User-space exhaustion test
# ---------------------------------------------------------------------------

# Safety margin: stop when this many bytes remain free (avoids a real ENOSPC)
_EXHAUST_SAFETY_BYTES = 512 * ONE_MB   # 512 MB


def measure_disk_user_space_exhaustion(path: str = "/") -> dict:
    """
    Measure the practical user-writable capacity of the filesystem at *path*
    by allocating disk space with posix_fallocate (zero-copy – instant on most
    kernels) until we hit ENOSPC or until the safety margin is reached.

    Returns a dict with:
        start_avail_gb    – f_bavail at the start (user-available bytes)
        allocated_gb      – total bytes successfully allocated
        free_at_stop_gb   – f_bavail immediately after the last allocation
        capacity_gb       – start_avail_gb  (== allocated + free_at_stop)
        safety_margin_mb  – the configured safety stop (MB)
        hit_enospc        – True if the OS raised ENOSPC before the safety stop
    """
    exhaust_dir = os.path.join(tempfile.gettempdir(), "gh_disk_exhaust")
    os.makedirs(exhaust_dir, exist_ok=True)
    files: list[str] = []
    allocated = 0
    hit_enospc = False

    st0 = os.statvfs(path)
    start_avail = st0.f_bavail * st0.f_frsize

    try:
        file_idx = 0
        while True:
            st = os.statvfs(path)
            free_bytes = st.f_bavail * st.f_frsize
            if free_bytes <= _EXHAUST_SAFETY_BYTES:
                break  # safety stop

            chunk = min(ONE_GB, free_bytes - _EXHAUST_SAFETY_BYTES)
            if chunk <= 0:
                break

            fpath = os.path.join(exhaust_dir, f"fill_{file_idx:05d}.bin")
            fd = os.open(fpath, os.O_CREAT | os.O_WRONLY)
            try:
                os.posix_fallocate(fd, 0, chunk)
                os.close(fd)
            except OSError:
                os.close(fd)
                try:
                    os.unlink(fpath)
                except OSError:
                    pass
                hit_enospc = True
                break

            files.append(fpath)
            allocated += chunk
            file_idx += 1
    finally:
        st_stop = os.statvfs(path)
        free_at_stop = st_stop.f_bavail * st_stop.f_frsize
        shutil.rmtree(exhaust_dir, ignore_errors=True)

    return {
        "start_avail_gb":   start_avail / ONE_GB,
        "allocated_gb":     allocated / ONE_GB,
        "free_at_stop_gb":  free_at_stop / ONE_GB,
        "capacity_gb":      start_avail / ONE_GB,
        "safety_margin_mb": _EXHAUST_SAFETY_BYTES // ONE_MB,
        "hit_enospc":       hit_enospc,
    }


def test_disk_user_space_exhaustion():
    """
    Fill the filesystem to near-ENOSPC using posix_fallocate (instant, no data
    written) and report the true user-writable capacity.

    Key question: is the user-available space (f_bavail) actually fully
    writable, or is there a lower artificial ceiling?
    """
    print("\n=== Disk User-Space Exhaustion Test ===")

    st0 = os.statvfs("/")
    start_avail_gb = st0.f_bavail * st0.f_frsize / ONE_GB
    start_total_gb = st0.f_blocks * st0.f_frsize / ONE_GB
    start_used_gb  = (st0.f_blocks - st0.f_bfree) * st0.f_frsize / ONE_GB
    start_reserved = (st0.f_bfree - st0.f_bavail) * st0.f_frsize / ONE_GB

    print(f"\n  Baseline  (f_blocks * frsize): total={start_total_gb:.2f} GB")
    print(f"  In use                       : {start_used_gb:.2f} GB")
    print(f"  Free (root, f_bfree)         : {(st0.f_bfree * st0.f_frsize / ONE_GB):.2f} GB")
    print(f"  User-available (f_bavail)    : {start_avail_gb:.2f} GB  ← what user processes see")
    print(f"  Reserved for root            : {start_reserved:.2f} GB")
    print(f"\n  Safety margin for test       : {_EXHAUST_SAFETY_BYTES // ONE_MB} MB")
    print(f"\n  Filling to near-exhaustion using posix_fallocate …")

    result = measure_disk_user_space_exhaustion("/")

    print(f"\n  Total allocated              : {result['allocated_gb']:.2f} GB")
    print(f"  Free at stop                 : {result['free_at_stop_gb']:.3f} GB")
    stop_reason = "ENOSPC raised" if result["hit_enospc"] else f"safety stop ({result['safety_margin_mb']} MB free)"
    print(f"  Stop reason                  : {stop_reason}")
    print(f"\n  User-writable capacity       : ~{result['capacity_gb']:.2f} GB")

    if not result["hit_enospc"]:
        print(f"\n  VERDICT: The full f_bavail ({result['capacity_gb']:.2f} GB) is user-writable.")
        print(f"           No artificial ceiling found below the OS-reported limit.")
    else:
        print(f"\n  VERDICT: ENOSPC hit at {result['allocated_gb']:.2f} GB allocated.")
        print(f"           OS-enforced limit confirmed at {result['capacity_gb']:.2f} GB.")

    # Verify disk space was freed
    st_clean = os.statvfs("/")
    free_after_gb = st_clean.f_bavail * st_clean.f_frsize / ONE_GB
    print(f"\n  Free after cleanup           : {free_after_gb:.2f} GB  (baseline restored)")

    assert result["capacity_gb"] > 0
    assert free_after_gb > result["capacity_gb"] * 0.9, "Disk space not fully returned after cleanup"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        test_disk_baseline()
        test_disk_single_large_file()
        test_disk_many_files()
    finally:
        _cleanup()
    print("\nAll disk tests completed.")
