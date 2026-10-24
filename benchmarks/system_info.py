"""System and process resource inspection utilities for CandGuard benchmarks."""
from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys
from typing import Any


def get_system_info() -> dict[str, Any]:
    """Collect platform, processor, RAM, disk, Python, and SQLite details."""
    total_ram_gb: float | None = None
    free_ram_gb: float | None = None

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("sullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_ram_gb = round(stat.ullTotalPhys / (1024**3), 2)
                free_ram_gb = round(stat.ullAvailPhys / (1024**3), 2)
        except Exception:
            pass
    elif hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total_ram_gb = round((pages * page_size) / (1024**3), 2)
        except Exception:
            pass

    disk_free_gb = round(shutil.disk_usage(".").free / (1024**3), 2)

    return {
        "os": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "total_ram_gb": total_ram_gb,
        "free_ram_gb": free_ram_gb,
        "disk_free_gb": disk_free_gb,
        "python_version": sys.version.split()[0],
        "sqlite_version": sqlite3.sqlite_version,
    }


def get_peak_memory_mb() -> float:
    """Return peak resident memory (RSS) in megabytes for the current process."""
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD),
                    ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.windll.psapi
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wt.DWORD,
            ]
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return round(pmc.PeakWorkingSetSize / (1024 * 1024), 2)
        except Exception:
            pass

    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is in KiB; on macOS it is in bytes
        multiplier = 1 / (1024 * 1024) if sys.platform == "darwin" else 1 / 1024
        return round(ru.ru_maxrss * multiplier, 2)
    except (ImportError, AttributeError):
        pass

    return 0.0
