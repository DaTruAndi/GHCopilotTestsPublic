"""
Disk Space Environment Tests
==============================
Hypotheses to test:
  - Is there a hard 14 GB disk limit enforced by the container/hypervisor?
  - Can we write a single large file beyond 14 GB?
  - Can we write many 100 MB files that together exceed 14 GB?

Strategy
--------
1. Report current disk usage/free for all relevant mount points
2. Single-file growth test: grow one file in 1 GB increments up to ~20 GB,
   documenting free space before each step
3. Multi-file test: write 100 MB files until a limit is hit or we reach 20 GB
4. Clean up all test files
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
