"""Canonical kernel process-start identity shared by release peers."""

from __future__ import annotations

import ctypes
from pathlib import Path
import struct
import sys


def process_start_token(pid: int) -> str | None:
    """Return an exact, platform-tagged PID-reuse guard."""
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return None
    if normalized_pid <= 1:
        return None
    if sys.platform == "darwin":
        try:
            buffer_size = 136
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(buffer_size)
            returned = proc_pidinfo(
                normalized_pid,
                3,
                0,
                buffer,
                buffer_size,
            )
            if returned != buffer_size:
                return None
            seconds, microseconds = struct.unpack_from("=QQ", buffer.raw, 120)
            if seconds <= 0 or microseconds >= 1_000_000:
                return None
            return (
                f"darwin-proc:{normalized_pid}:"
                f"{seconds}:{microseconds}"
            )
        except (AttributeError, OSError, TypeError, ValueError, struct.error):
            return None
    proc_stat = Path(f"/proc/{normalized_pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        start_ticks = int(fields[19])
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None
    return f"procfs:{normalized_pid}:{start_ticks}"
