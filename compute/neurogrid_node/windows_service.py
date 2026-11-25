"""The Windows SCM entry point for the enrolled contributor process."""

import ctypes
from collections.abc import Callable
from ctypes import wintypes
from threading import Event, Lock

NAME = "NeuroGridContributor"
START_PENDING, STOP_PENDING, RUNNING, STOPPED = 2, 3, 4, 1
STOP, SHUTDOWN, INTERROGATE = 1, 5, 4


def dispatch(run: Callable[[], int], stop: Event) -> int:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    dword = wintypes.DWORD
    main_callback = ctypes.WINFUNCTYPE(None, dword, ctypes.POINTER(wintypes.LPWSTR))
    control_callback = ctypes.WINFUNCTYPE(dword, dword, dword, ctypes.c_void_p, ctypes.c_void_p)

    class Status(ctypes.Structure):
        _fields_ = [(name, dword) for name in (
            "type", "state", "accepted", "system_exit", "service_exit", "checkpoint", "wait_hint",
        )]

    class Entry(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("callback", main_callback)]

    advapi.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, control_callback, ctypes.c_void_p]
    advapi.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    advapi.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(Status)]
    advapi.SetServiceStatus.restype = wintypes.BOOL
    advapi.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(Entry)]
    advapi.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
    handle = None
    status = Status(0x10, START_PENDING, 0, 0, 0, 1, 15_000)
    lock = Lock()

    def publish(state: int, code: int = 0):
        with lock:
            status.state = state
            status.accepted = 0x05 if state == RUNNING else 0
            status.system_exit = 1066 if code else 0
            status.service_exit = code
            status.checkpoint = 1 if state in (START_PENDING, STOP_PENDING) else 0
            status.wait_hint = 30_000 if status.checkpoint else 0
            advapi.SetServiceStatus(handle, ctypes.byref(status))

    @control_callback
    def control(request, _event, _data, _context):
        if request in (STOP, SHUTDOWN):
            publish(STOP_PENDING)
            stop.set()
        elif request == INTERROGATE:
            with lock:
                advapi.SetServiceStatus(handle, ctypes.byref(status))
        return 0

    @main_callback
    def service_main(_argc, _argv):
        nonlocal handle
        handle = advapi.RegisterServiceCtrlHandlerExW(NAME, control, None)
        if not handle:
            return
        publish(START_PENDING)
        publish(RUNNING)
        code = 1
        try:
            code = run()
        finally:
            publish(STOPPED, 0 if stop.is_set() or code == 78 else code)

    table = (Entry * 2)(Entry(NAME, service_main), Entry(None, main_callback()))
    if not advapi.StartServiceCtrlDispatcherW(table):
        print(f"Windows SCM dispatcher could not attach (error {ctypes.get_last_error()})", flush=True)
        return 1
    return 0
