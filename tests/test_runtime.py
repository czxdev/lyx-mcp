import asyncio
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lyx_mcp.config import Config
from lyx_mcp.runtime import Layout, Runtime

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process supervision")


def _live(pid: int) -> bool:
    status = Path(f"/proc/{pid}/stat")
    return status.exists() and status.read_text().split(") ", 1)[1][0] != "Z"


def test_lyx_launcher_dies_when_mcp_parent_is_killed() -> None:
    parent_code = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-m', 'lyx_mcp.launch', str(os.getpid()), "
        "sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    try:
        time.sleep(0.3)
        assert _live(child_pid)
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 3
        while _live(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _live(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if _live(child_pid):
            os.killpg(child_pid, signal.SIGKILL)


def test_runtime_close_kills_stubborn_process_group(tmp_path: Path) -> None:
    child_code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code], stdout=subprocess.PIPE, start_new_session=True
    )
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline())
    root = tmp_path / "runtime"
    root.mkdir()
    runtime = Runtime(Config())
    runtime.process = leader
    runtime.layout = Layout(root, root, root, root, root, root, root / "pipe")
    try:
        asyncio.run(runtime.close())
        assert not _live(leader.pid)
        assert not _live(child_pid)
        assert not root.exists()
    finally:
        if _live(leader.pid) or _live(child_pid):
            os.killpg(leader.pid, signal.SIGKILL)
        if leader.poll() is None:
            leader.wait(timeout=5)


@pytest.mark.skipif(shutil.which("lyx") is None, reason="LyX is not installed")
def test_real_lyx_exits_when_mcp_parent_is_killed(tmp_path: Path) -> None:
    parent_code = (
        "import asyncio\n"
        "from lyx_mcp.config import Config\n"
        "from lyx_mcp.runtime import Runtime\n"
        "from pathlib import Path\n"
        "async def main():\n"
        f"    runtime = Runtime(Config(runtime_root=Path({str(tmp_path)!r})))\n"
        "    await runtime.start()\n"
        "    print(runtime.process.pid, flush=True)\n"
        "    await asyncio.Event().wait()\n"
        "asyncio.run(main())\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert parent.stdout is not None
    assert parent.stderr is not None
    lyx_pid = None
    try:
        readable, _, _ = select.select([parent.stdout], [], [], 50)
        assert readable, "isolated LyX did not start"
        line = parent.stdout.readline().strip()
        assert line, parent.stderr.read().decode(errors="replace")
        lyx_pid = int(line)
        assert _live(lyx_pid)
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _live(lyx_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _live(lyx_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if lyx_pid is not None and _live(lyx_pid):
            os.killpg(lyx_pid, signal.SIGKILL)
