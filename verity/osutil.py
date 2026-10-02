"""The few operations that differ between macOS/Linux and Windows, in one place: executable names, whether a process is
alive, stopping it, what command it runs, an exclusive file lock, and physical memory.

On Windows `os.kill(pid, 0)` does not test a process — it TERMINATES it — so liveness goes through OpenProcess, and
there is no fcntl, SIGKILL or `ps`."""
from __future__ import annotations

import ctypes
import functools
import os
import signal
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def exe(path: Path) -> Path:
    """`tools/llama.cpp/llama-server` → `llama-server.exe` on Windows (an explicit suffix or an existing file wins)."""
    path = Path(path)
    if IS_WINDOWS and not path.suffix and not path.exists():
        return path.with_name(path.name + ".exe")
    return path


@functools.lru_cache(maxsize=1)
def mac_dev_tools() -> bool:
    """macOS: True when Xcode or its Command Line Tools are installed. Without them /usr/bin/clang, cc and xcrun are
    stubs that open an "install developer tools" dialog, so check this before running them. True elsewhere."""
    if sys.platform != "darwin":
        return True
    try:
        p = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(p) and os.path.isdir(p)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        k32 = ctypes.windll.kernel32                                   # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, pid)                   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return k32.GetLastError() == 5                             # ERROR_ACCESS_DENIED: it exists
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                                   # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_command(pid: int) -> str:
    """The command line (Unix) or image name (Windows) of a running process; empty when unknown."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            return out.split(",")[0].strip('"') if out.startswith('"') else ""
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def terminate(pid: int, force: bool = False) -> None:
    """Ask a process to stop (force: kill it). Windows stops the whole process tree."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else []), capture_output=True,
                       timeout=15)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def stdin_closed() -> bool:
    """True once the other end of our stdin pipe has been closed (the desktop app quit or was killed). Checked without
    reading: on Windows a thread blocked reading a pipe stalls the start of child processes, such as the parser
    workers. False when stdin is not a pipe or cannot be checked."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    if IS_WINDOWS:
        import msvcrt
        from ctypes import wintypes
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)          # type: ignore[attr-defined]
        peek = k32.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        avail = wintypes.DWORD(0)
        if peek(handle, None, 0, None, ctypes.byref(avail), None):
            return False
        return ctypes.get_last_error() == 109                         # ERROR_BROKEN_PIPE: the writer is gone
    import select
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        return bool(ready) and os.read(fd, 65536) == b""              # readable with no data = end of file
    except OSError:
        return False


def lock_exclusive(fh) -> None:
    """Non-blocking exclusive lock on an open file; raises OSError when another process holds it."""
    if IS_WINDOWS:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fh) -> None:
    if IS_WINDOWS:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def windows_memory() -> tuple[int, int]:
    """(total, available) physical memory in bytes on Windows; (0, 0) elsewhere or on failure."""
    if not IS_WINDOWS:
        return 0, 0
    st = _MemoryStatus()
    st.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):   # type: ignore[attr-defined]
        return 0, 0
    return int(st.ullTotalPhys), int(st.ullAvailPhys)
