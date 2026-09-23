from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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

    async def start(self) -> None:
        if self.process is not None:
            return
        self._check_binary()
        root = Path(tempfile.mkdtemp(prefix="lyx-mcp-", dir=self.config.runtime_root))
        root.chmod(0o700)
        layout = Layout(
            root=root,
            userdir=root / "userdir",
            temp=root / "tmp",
            logs=root / "logs",
            exports=root / "exports",
            snapshots=root / "snapshots",
            pipe_stem=root / "userdir" / "run" / "lyxserver",
        )
        self.layout = layout
        for directory in (layout.userdir, layout.temp, layout.logs, layout.exports, layout.snapshots):
            directory.mkdir(mode=0o700, exist_ok=True)
        self._prepare_userdir(layout)
        environment = os.environ.copy()
        environment = {key: value for key, value in environment.items() if not key.startswith("LYX_USERDIR_")}
        environment.update(
            QT_QPA_PLATFORM="offscreen",
            TMPDIR=str(layout.temp),
            XDG_CACHE_HOME=str(root / "cache"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
        )
        with (layout.logs / "stdout.log").open("wb") as out, (layout.logs / "stderr.log").open("wb") as err:
            self.process = subprocess.Popen(
                [self.config.lyx_binary, "--no-remote", "-userdir", str(layout.userdir)],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=environment,
                start_new_session=True,
            )
        try:
            await self._wait_for_pipes(layout)
            client = LyXClient(layout.pipe_stem, f"lyx-mcp-{uuid.uuid4().hex[:12]}", self.config.command_timeout_sec)
            client.connect()
            self.client = client
            await client.hello()
            await client.call("server-get-filename")
        except BaseException:
            await self.close(preserve=True)
            raise

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
            if process.poll() is None:
                process.terminate()
            deadline = time.monotonic() + 3
            while process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if process.poll() is None:
                process.kill()
                process.wait()
            self.process = None
        if self.layout is not None and not preserve:
            shutil.rmtree(self.layout.root)
            self.layout = None
