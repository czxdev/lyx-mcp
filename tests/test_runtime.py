import asyncio
import fcntl
import gc
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lyx_mcp.config import Config
from lyx_mcp.errors import LyXMCPError
from lyx_mcp.runtime import Layout, Runtime

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process supervision")


def _live(pid: int) -> bool:
    status = Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def test_runtime_reuses_slot_and_keeps_lock_file(tmp_path: Path) -> None:
    roots = []
    lock_inodes = []
    for _ in range(3):
        runtime = Runtime(Config(runtime_root=tmp_path))
        try:
            layout = runtime._prepare_layout()
            roots.append(layout.root)
            lock = layout.root.with_suffix(".lock")
            lock_inodes.append(lock.stat().st_ino)
            assert stat.S_IMODE(layout.root.stat().st_mode) == 0o700
            assert stat.S_IMODE(layout.root.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
            (layout.exports / "export.pdf").write_bytes(b"export")
        finally:
            asyncio.run(runtime.close())
        assert not layout.root.exists()
    assert len(set(roots)) == 1
    assert len(set(lock_inodes)) == 1
    assert list(roots[0].parent.iterdir()) == [roots[0].with_suffix(".lock")]


def test_discarded_runtime_releases_slot(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)
    runtime = Runtime(config)
    layout = runtime._prepare_layout()
    del runtime
    gc.collect()
    replacement = Runtime(config)
    try:
        assert replacement._prepare_layout().root == layout.root
    finally:
        asyncio.run(replacement.close())


def test_concurrent_processes_claim_separate_slots_and_reclaim_stale_files(tmp_path: Path) -> None:
    worker_code = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from lyx_mcp.config import Config\n"
        "from lyx_mcp.runtime import Runtime\n"
        f"runtime = Runtime(Config(runtime_root=Path({str(tmp_path)!r})))\n"
        "layout = runtime._prepare_layout()\n"
        "(layout.exports / 'owner').write_text(str(os.getpid()))\n"
        "(layout.snapshots / 'stale').write_text('old snapshot')\n"
        "(layout.temp / 'stale').write_text('old compilation')\n"
        "os.mkfifo(str(layout.pipe_stem) + '.in')\n"
        "print(layout.root, flush=True)\n"
        "time.sleep(60)\n"
    )
    workers = []
    roots = []
    try:
        for _ in range(4):
            workers.append(subprocess.Popen([sys.executable, "-c", worker_code], stdout=subprocess.PIPE))
        for worker in workers:
            assert worker.stdout is not None
            ready, _, _ = select.select([worker.stdout], [], [], 5)
            assert ready, "runtime slot was not claimed"
            root = Path(worker.stdout.readline().decode().strip())
            roots.append(root)
            assert (root / "exports" / "owner").read_text() == str(worker.pid)
        assert len(set(roots)) == len(workers)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
    runtime = Runtime(Config(runtime_root=tmp_path))
    try:
        layout = runtime._prepare_layout()
        assert layout.root == roots[0].parent / "0"
        assert not (layout.exports / "owner").exists()
        assert not (layout.snapshots / "stale").exists()
        assert not (layout.temp / "stale").exists()
        assert not Path(f"{layout.pipe_stem}.in").exists()
        assert len(list(layout.root.parent.glob("*.lock"))) == len(workers)
    finally:
        asyncio.run(runtime.close())


def test_inherited_lock_protects_slot_after_parent_is_killed(tmp_path: Path) -> None:
    parent_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "from lyx_mcp.config import Config\n"
        "from lyx_mcp.runtime import Runtime\n"
        f"runtime = Runtime(Config(runtime_root=Path({str(tmp_path)!r})))\n"
        "layout = runtime._prepare_layout()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "pass_fds=(runtime._lock_file.fileno(),), start_new_session=True)\n"
        "print(str(layout.root) + '\\t' + str(child.pid), flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE)
    child_pid = None
    first = Runtime(Config(runtime_root=tmp_path))
    second = Runtime(Config(runtime_root=tmp_path))
    try:
        assert parent.stdout is not None
        ready, _, _ = select.select([parent.stdout], [], [], 5)
        assert ready, "lock holder did not start"
        root_text, pid_text = parent.stdout.readline().decode().strip().split("\t")
        root = Path(root_text)
        child_pid = int(pid_text)
        parent.kill()
        parent.wait(timeout=5)
        assert _live(child_pid)
        assert first._prepare_layout().root != root
        os.killpg(child_pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while _live(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _live(child_pid)
        assert second._prepare_layout().root == root
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None and _live(child_pid):
            os.killpg(child_pid, signal.SIGKILL)
        asyncio.run(first.close())
        asyncio.run(second.close())


@pytest.mark.parametrize("unsafe", ["symlink", "permissions"])
def test_runtime_rejects_unsafe_pool(tmp_path: Path, unsafe: str) -> None:
    pool = tmp_path / f"lyx-mcp-{os.getuid()}"
    if unsafe == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        (target / "keep").write_text("keep")
        pool.symlink_to(target, target_is_directory=True)
    else:
        pool.mkdir()
        pool.chmod(0o755)
    runtime = Runtime(Config(runtime_root=tmp_path))
    with pytest.raises(LyXMCPError) as error:
        runtime._prepare_layout()
    assert error.value.code == "UNSAFE_RUNTIME_ROOT"
    assert runtime._lock_file is None
    if unsafe == "symlink":
        assert (target / "keep").read_text() == "keep"


@pytest.mark.parametrize("failure", ["profile", "binary"])
def test_failed_start_reuses_slot_and_can_be_closed(tmp_path: Path, failure: str) -> None:
    seed = tmp_path / "seed"
    if failure == "binary":
        seed.mkdir()
    runtime = Runtime(Config(runtime_root=tmp_path, profile_seed_dir=seed, lyx_binary=str(tmp_path / "missing-lyx")))

    async def exercise() -> None:
        roots = []
        try:
            for _ in range(2):
                expected = FileNotFoundError if failure == "profile" else LyXMCPError
                with pytest.raises(expected):
                    await runtime.start()
                assert runtime.layout is not None
                roots.append(runtime.layout.root)
                assert runtime.process is None
            assert roots[0] == roots[1]
            assert len(list(roots[0].parent.glob("*.lock"))) == 1
        finally:
            await runtime.close()
        assert not roots[0].exists()
        assert runtime._lock_file is None

    asyncio.run(exercise())


@pytest.mark.skipif(shutil.which("lyx") is None, reason="LyX is not installed")
def test_real_lyx_restarts_reuse_slot_and_preserve_artifacts(tmp_path: Path) -> None:
    async def exercise() -> None:
        runtime = Runtime(Config(runtime_root=tmp_path))
        await runtime.start()
        assert runtime.layout is not None
        layout = runtime.layout
        assert runtime._lock_file is not None
        lock_fd = runtime._lock_file.fileno()
        (layout.exports / "export.pdf").write_bytes(b"export")
        (layout.snapshots / "document.lyx").write_bytes(b"snapshot")
        try:
            for _ in range(2):
                assert runtime.process is not None
                pid = runtime.process.pid
                assert Path(f"/proc/{pid}/fd/{lock_fd}").resolve() == layout.root.with_suffix(".lock")
                stale = [layout.userdir, layout.temp, layout.logs]
                for name in ("cache", "config", "data"):
                    directory = layout.root / name
                    directory.mkdir(exist_ok=True)
                    stale.append(directory)
                for directory in stale:
                    (directory / "stale").write_bytes(b"stale")
                await runtime.restart()
                assert runtime.layout == layout
                assert runtime.process is not None and runtime.process.pid != pid
                assert runtime._lock_file is not None and runtime._lock_file.fileno() == lock_fd
                assert all(not (directory / "stale").exists() for directory in stale)
                assert (layout.exports / "export.pdf").read_bytes() == b"export"
                assert (layout.snapshots / "document.lyx").read_bytes() == b"snapshot"
                with layout.root.with_suffix(".lock").open("rb") as lock:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert len(list(layout.root.parent.glob("*.lock"))) == 1
        finally:
            await runtime.close()
        assert not layout.root.exists()

    asyncio.run(exercise())


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
