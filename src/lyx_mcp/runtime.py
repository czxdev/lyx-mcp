from __future__ import annotations

import asyncio
import fcntl
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .client import LyXClient
from .config import Config
from .errors import LyXMCPError


@dataclass(frozen=True, slots=True)
class Layout:
    root: Path
    userdir: Path
    temp: Path
    logs: Path
    exports: Path
    snapshots: Path
    pipe_stem: Path


class Runtime:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.layout: Layout | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.client: LyXClient | None = None
        self._lock_file: BinaryIO | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        self._check_binary()
        try:
            layout = self._prepare_layout()
            environment = os.environ.copy()
            environment = {key: value for key, value in environment.items() if not key.startswith("LYX_USERDIR_")}
            environment.update(
                QT_QPA_PLATFORM="offscreen",
                TMPDIR=str(layout.temp),
                XDG_CACHE_HOME=str(layout.root / "cache"),
                XDG_CONFIG_HOME=str(layout.root / "config"),
                XDG_DATA_HOME=str(layout.root / "data"),
            )
            assert self._lock_file is not None
            with (layout.logs / "stdout.log").open("wb") as out, (layout.logs / "stderr.log").open("wb") as err:
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "lyx_mcp.launch",
                        str(os.getpid()),
                        self.config.lyx_binary,
                        "--no-remote",
                        "-userdir",
                        str(layout.userdir),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=environment,
                    start_new_session=True,
                    pass_fds=(self._lock_file.fileno(),),
                )
            await self._wait_for_pipes(layout)
            client = LyXClient(layout.pipe_stem, f"lyx-mcp-{uuid.uuid4().hex[:12]}", self.config.command_timeout_sec)
            client.connect()
            self.client = client
            await client.hello()
            await client.call("server-get-filename")
        except BaseException:
            await self.close(preserve=True)
            raise

    def _acquire_root(self) -> Path:
        pool = self.config.runtime_root / f"lyx-mcp-{os.getuid()}"
        pool.mkdir(mode=0o700, exist_ok=True)
        details = pool.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise LyXMCPError("UNSAFE_RUNTIME_ROOT", f"Runtime pool must be a private, user-owned directory: {pool}")
        slot = 0
        while True:
            lock_file = os.fdopen(
                os.open(pool / f"{slot}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600), "r+b"
            )
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                lock_file.close()
                if isinstance(exc, BlockingIOError):
                    slot += 1
                    continue
                raise
            self._lock_file = lock_file
            return pool / str(slot)

    def _prepare_layout(self) -> Layout:
        reset: tuple[Path, ...]
        if self.layout is None:
            root = self._acquire_root()
            self.layout = Layout(
                root=root,
                userdir=root / "userdir",
                temp=root / "tmp",
                logs=root / "logs",
                exports=root / "exports",
                snapshots=root / "snapshots",
                pipe_stem=root / "userdir" / "run" / "lyxserver",
            )
            reset = (root,)
        else:
            layout = self.layout
            reset = (
                layout.userdir,
                layout.temp,
                layout.logs,
                layout.root / "cache",
                layout.root / "config",
                layout.root / "data",
            )
        for directory in reset:
            if directory.exists():
                shutil.rmtree(directory)
        layout = self.layout
        layout.root.mkdir(mode=0o700, exist_ok=True)
        for directory in (layout.userdir, layout.temp, layout.logs, layout.exports, layout.snapshots):
            directory.mkdir(mode=0o700, exist_ok=True)
        self._prepare_userdir(layout)
        return layout

    def _check_binary(self) -> None:
        if self.config.profile_seed_dir is not None:
            return
        result = subprocess.run(
            [self.config.lyx_binary, "-version"], capture_output=True, text=True, check=True, timeout=10
        )
        if not re.search(r"LyX 2\.4\.", result.stdout):
            raise LyXMCPError(
                "UNSUPPORTED_LYX_VERSION",
                "The built-in profile is verified for LyX 2.4.x; configure profile_seed_dir for another version",
            )

    def _prepare_userdir(self, layout: Layout) -> None:
        if self.config.profile_seed_dir is not None:
            shutil.copytree(
                self.config.profile_seed_dir,
                layout.userdir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("run", "*.in", "*.out"),
            )
        (layout.userdir / "run").mkdir(mode=0o700, exist_ok=True)
        preferences = layout.userdir / "preferences"
        contents = preferences.read_text(encoding="utf-8") if preferences.exists() else "Format 38\n"
        contents = "\n".join(
            line
            for line in contents.splitlines()
            if not line.startswith(
                (
                    "\\serverpipe ",
                    "\\auto_region_delete ",
                    "\\load_session ",
                    "\\plaintext_linelen ",
                    "\\make_backup ",
                )
            )
        )
        contents += (
            '\n\\serverpipe "$$UserDir/run/lyxserver"\n'
            "\\auto_region_delete true\n\\load_session false\n"
            "\\plaintext_linelen 10000\n\\make_backup false\n"
        )
        preferences.write_text(contents, encoding="utf-8")
        preferences.chmod(0o600)

    async def _wait_for_pipes(self, layout: Layout) -> None:
        assert self.process is not None
        deadline = time.monotonic() + self.config.startup_timeout_sec
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LyXMCPError("LYX_START_FAILED", self._stderr_tail())
            incoming = Path(f"{layout.pipe_stem}.in")
            outgoing = Path(f"{layout.pipe_stem}.out")
            if incoming.exists() and outgoing.exists():
                if stat.S_ISFIFO(incoming.stat().st_mode) and stat.S_ISFIFO(outgoing.stat().st_mode):
                    return
                raise LyXMCPError("LYX_START_FAILED", "LyXServer endpoints are not FIFOs")
            await asyncio.sleep(0.1)
        raise LyXMCPError("LYX_START_TIMEOUT", self._stderr_tail())

    def _stderr_tail(self) -> str:
        assert self.layout is not None
        return (self.layout.logs / "stderr.log").read_text(errors="replace")[-2000:]

    async def restart(self) -> None:
        await self.close(preserve=True)
        await self.start()

    async def close(self, preserve: bool = False) -> None:
        process = self.process
        if process is not None and process.poll() is None and self.client is not None:
            try:
                await self.client.call("lyx-quit", timeout=3)
            except (LyXMCPError, OSError):
                pass
        if self.client is not None:
            self.client.close()
            self.client = None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while self._group_exists(process.pid) and time.monotonic() < deadline:
                process.poll()
                await asyncio.sleep(0.05)
            if self._group_exists(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.wait()
            self.process = None
        if preserve:
            return
        if self.layout is not None:
            if self.layout.root.exists():
                shutil.rmtree(self.layout.root)
            self.layout = None
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True
