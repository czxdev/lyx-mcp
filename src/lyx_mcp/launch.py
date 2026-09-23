from __future__ import annotations

import ctypes
import os
import signal
import sys


def main() -> None:
    parent_pid = int(sys.argv[1])
    binary = sys.argv[2]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent_pid:
        raise RuntimeError("MCP server exited before LyX started")
    os.execvpe(binary, sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
