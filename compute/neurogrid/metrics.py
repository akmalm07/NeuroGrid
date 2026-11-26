"""Process memory counters shared by native and WASI benchmark runners."""

import ctypes
import os
import sys
from subprocess import Popen


def peak_child(process: Popen) -> dict[str, int | None]:
    unavailable = {"peak_resident_bytes": None, "peak_commit_bytes": None}
    if os.name != "nt":
        return unavailable
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("faults", wintypes.DWORD),
            ("peak_working_set", ctypes.c_size_t),
            ("working_set_and_pools", ctypes.c_size_t * 6),
            ("peak_commit", ctypes.c_size_t),
        ]

    info = Counters()
    info.size = ctypes.sizeof(info)
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    get_memory.restype = wintypes.BOOL
    if get_memory(int(process._handle), ctypes.byref(info), info.size):
        return {"peak_resident_bytes": info.peak_working_set, "peak_commit_bytes": info.peak_commit}
    return unavailable


def peak_memory() -> dict[str, int | None]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD), ("faults", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t), ("remaining", ctypes.c_size_t * 6),
                ("peak_commit", ctypes.c_size_t),
            ]

        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
        get_memory.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.size = ctypes.sizeof(counters)
        if not get_memory(current_process(), ctypes.byref(counters), counters.size):
            raise OSError("GetProcessMemoryInfo failed")
        return {"peak_resident_bytes": counters.peak_working_set, "peak_commit_bytes": counters.peak_commit}

    import resource

    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"peak_resident_bytes": maximum if sys.platform == "darwin" else maximum * 1024,
            "peak_commit_bytes": None}
