"""Locate the main Antigravity window (geometry only, never its content)."""

from __future__ import annotations

import sys
import time

from .autonomy import WindowTarget

PROCESS_NAMES = {"antigravity.exe"}


class AntigravityWindowFinder:
    def __init__(self, rect_reader=None, *, cache_seconds: float = 3.0) -> None:
        self.available = sys.platform == "win32"
        self.cache_seconds = cache_seconds
        self._pids: set[int] = set()
        self._pids_at = -1e9
        self._result: WindowTarget | None = None
        self._result_at = -1e9
        if rect_reader is None and self.available:
            from pet.surfaces import Win32WindowRectReader

            rect_reader = Win32WindowRectReader()
        self.reader = rect_reader

    def _antigravity_pids(self, now: float) -> set[int]:
        if now - self._pids_at < 30.0:
            return self._pids
        self._pids_at = now
        try:
            import psutil

            self._pids = {
                p.pid for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() in PROCESS_NAMES
            }
        except Exception:
            self._pids = set()
        return self._pids

    def find(self) -> WindowTarget | None:
        """Largest visible, non-minimized top-level window of Antigravity."""
        now = time.monotonic()
        if now - self._result_at < self.cache_seconds:
            return self._result
        self._result_at = now
        self._result = None
        if not self.available or self.reader is None:
            return None
        pids = self._antigravity_pids(now)
        if not pids:
            return None
        import ctypes
        from ctypes import wintypes

        if not hasattr(self, "_user32"):
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            self._enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [self._enum_proc_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            self._user32 = user32

        user32 = self._user32
        handles: list[int] = []

        @self._enum_proc_type
        def collect(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                handles.append(int(ctypes.cast(hwnd, ctypes.c_void_p).value or 0))
            return True

        user32.EnumWindows(collect, 0)
        best: WindowTarget | None = None
        for handle in handles:
            rect = self.reader.read(handle)  # None when minimized
            if rect is None:
                continue
            target = WindowTarget(handle, *rect)
            if best is None or target.width * (target.bottom - target.top) > best.width * (best.bottom - best.top):
                best = target
        self._result = best
        return best
