"""
Creality Print lifecycle helpers (Windows, standard library only).

Provides:
  - `is_process_running(names)`: allocation-free Toolhelp32 process scan (no
    `tasklist` spawning, safe to call every 1-2 seconds).
  - `CrealityPrintWatcher`: background thread firing `on_open` / `on_close`
    callbacks when `CrealityPrint.exe` starts or exits (with a grace window
    so a quick application restart does not tear everything down).
  - `acquire_single_instance(name)`: named mutex guard so only one supervisor
    process exists even while it is dormant (no listening port).
  - `kill_process_tree(pid)`: terminates a process together with all of its
    children (needed because `.venv\\Scripts\\python.exe` is a launcher that
    spawns the real interpreter as a child process).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, Iterable, Optional, Set

CREALITY_PROCESS_NAMES = ("crealityprint.exe",)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _MAX_PATH = 260

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * _MAX_PATH),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    _ERROR_ALREADY_EXISTS = 183


def list_processes() -> list:
    """Return `(pid, parent_pid, exe_name_lower)` tuples for all running processes."""
    if not _IS_WINDOWS:
        return []
    out = []
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile).lower()))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def find_pids(names: Iterable[str]) -> Set[int]:
    wanted = {n.lower() for n in names}
    return {pid for pid, _ppid, exe in list_processes() if exe in wanted}


def is_process_running(names: Iterable[str] = CREALITY_PROCESS_NAMES) -> bool:
    if not _IS_WINDOWS:
        return False
    wanted = {n.lower() for n in names}
    for _pid, _ppid, exe in list_processes():
        if exe in wanted:
            return True
    return False


def descendant_pids(root_pid: int) -> Set[int]:
    procs = list_processes()
    children = {}
    for pid, ppid, _exe in procs:
        children.setdefault(ppid, []).append(pid)
    found: Set[int] = set()
    stack = [int(root_pid)]
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch not in found and ch != root_pid:
                found.add(ch)
                stack.append(ch)
    return found


def kill_process_tree(pid: Optional[int], timeout: float = 4.0) -> bool:
    """Force-terminate `pid` and all of its descendants. Returns True if a kill was issued."""
    if not pid:
        return False
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            os.kill(int(pid), 9)
        return True
    except Exception:
        return False


_mutex_handles = []


def acquire_single_instance(name: str) -> bool:
    """Create a named mutex. Returns False if another process already owns the name."""
    if not _IS_WINDOWS:
        return True
    handle = _kernel32.CreateMutexW(None, False, name)
    last_err = ctypes.get_last_error()
    if not handle:
        return True  # Fail open: never block startup because of a mutex API failure
    if last_err == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _mutex_handles.append(handle)  # Keep alive for the whole process lifetime
    return True


class CrealityPrintWatcher(threading.Thread):
    """
    Polls for Creality Print and fires callbacks on lifecycle transitions.

    - `on_open()` fires once when Creality Print is detected (also at startup if
      it is already running).
    - `on_close()` fires once Creality Print has been absent for `close_grace`
      seconds after having been seen running.
    """

    def __init__(
        self,
        on_open: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        interval: float = 2.0,
        close_grace: float = 5.0,
        names: Iterable[str] = CREALITY_PROCESS_NAMES,
    ) -> None:
        super().__init__(name="CrealityPrintWatcher", daemon=True)
        self.on_open = on_open
        self.on_close = on_close
        self.interval = max(0.5, float(interval))
        self.close_grace = max(0.0, float(close_grace))
        self.names = tuple(names)
        self.cp_running = False
        self.ever_seen = False
        self._absent_since = 0.0
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def _fire(self, cb: Optional[Callable[[], None]]) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    def run(self) -> None:
        while not self._stop_evt.is_set():
            running = is_process_running(self.names)
            now = time.monotonic()
            if running:
                self._absent_since = 0.0
                if not self.cp_running:
                    self.cp_running = True
                    self.ever_seen = True
                    self._fire(self.on_open)
            elif self.cp_running:
                if not self._absent_since:
                    self._absent_since = now
                if (now - self._absent_since) >= self.close_grace:
                    self.cp_running = False
                    self._absent_since = 0.0
                    self._fire(self.on_close)
            self._stop_evt.wait(self.interval)


def start_orphan_guard(shutdown_cb: Callable[[], None], close_grace: float = 8.0) -> Optional[CrealityPrintWatcher]:
    """
    Defense-in-depth for the `:8088` servers: if Creality Print was observed
    running and then closes, invoke `shutdown_cb` so no printer-facing server
    remains alive in the background (even if the supervisor crashed or the
    server was started manually via the batch launcher).

    Disabled with environment variable `AGY_NO_CP_WATCH=1`.
    """
    if not _IS_WINDOWS or os.environ.get("AGY_NO_CP_WATCH", "").strip() in ("1", "true", "yes"):
        return None
    watcher = CrealityPrintWatcher(on_open=None, on_close=shutdown_cb, interval=2.5, close_grace=close_grace)
    watcher.start()
    return watcher

