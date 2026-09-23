from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from .errors import LyXMCPError
from .protocol import Response, bye, command, hello, parse


class LyXClient:
    def __init__(self, stem: Path, client_name: str, timeout: float) -> None:
        self.stem = stem
        self.client_name = client_name
        self.timeout = timeout
        self._read_fd: int | None = None
        self._write_fd: int | None = None
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._broken = False

    def connect(self) -> None:
        self._read_fd = os.open(f"{self.stem}.out", os.O_RDONLY | os.O_NONBLOCK)
        try:
            self._write_fd = os.open(f"{self.stem}.in", os.O_WRONLY | os.O_NONBLOCK)
        except BaseException:
            os.close(self._read_fd)
            self._read_fd = None
            raise

    async def hello(self) -> None:
        response = await self._exchange(hello(self.client_name), "LYXSRV", "hello")
        if response.kind != "LYXSRV":
            raise LyXMCPError("PROTOCOL_ERROR", "LyXServer did not acknowledge hello")

    async def call(self, function: str, argument: str = "", timeout: float | None = None) -> str:
        response = await self._exchange(command(self.client_name, function, argument), function, function, timeout)
        if response.kind == "ERROR":
            raise LyXMCPError("LYX_COMMAND_FAILED", f"{function}: {response.payload}")
        return response.payload

    async def _exchange(self, data: bytes, expected: str, function: str, timeout: float | None = None) -> Response:
        async with self._lock:
            if self._broken or self._read_fd is None or self._write_fd is None:
                raise LyXMCPError("SESSION_BROKEN", "LyXServer connection is unavailable")
            try:
                return await self._exchange_io(data, expected, function, timeout or self.timeout)
            except asyncio.CancelledError:
                self._broken = True
                raise

    async def _exchange_io(self, data: bytes, expected: str, function: str, timeout: float) -> Response:
        assert self._read_fd is not None and self._write_fd is not None
        deadline = time.monotonic() + timeout
        try:
            offset = 0
            while offset < len(data):
                if time.monotonic() >= deadline:
                    raise TimeoutError("LyXServer write timed out")
                try:
                    offset += os.write(self._write_fd, data[offset:])
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            while True:
                line = self._next_line()
                if line is not None:
                    response = parse(line)
                    if response.kind == "NOTIFY":
                        continue
                    if response.client != self.client_name:
                        raise LyXMCPError("PROTOCOL_ERROR", "unexpected LyXServer client identity")
                    if response.kind == "LYXSRV" and expected == "LYXSRV" and response.function == function:
                        return response
                    if response.kind in ("INFO", "ERROR") and response.function == function:
                        return response
                    raise LyXMCPError("PROTOCOL_ERROR", "unexpected LyXServer response order")
                if time.monotonic() >= deadline:
                    raise TimeoutError("LyXServer response timed out")
                try:
                    chunk = os.read(self._read_fd, 8192)
                    if not chunk:
                        raise EOFError("LyXServer output pipe closed")
                    self._buffer.extend(chunk)
                    if len(self._buffer) > 4_000_000:
                        raise LyXMCPError("PROTOCOL_ERROR", "LyXServer response is too large")
                except BlockingIOError:
                    await asyncio.sleep(0.01)
        except (OSError, EOFError, TimeoutError, LyXMCPError) as exc:
            if not (isinstance(exc, LyXMCPError) and exc.code == "LYX_COMMAND_FAILED"):
                self._broken = True
            if isinstance(exc, TimeoutError):
                raise LyXMCPError("LYX_TIMEOUT", str(exc)) from exc
            raise

    def _next_line(self) -> bytes | None:
        end = self._buffer.find(b"\n")
        if end < 0:
            return None
        line = bytes(self._buffer[: end + 1])
        del self._buffer[: end + 1]
        return line

    def close(self) -> None:
        if self._write_fd is not None:
            if not self._broken:
                try:
                    os.write(self._write_fd, bye(self.client_name))
                except OSError:
                    pass
            os.close(self._write_fd)
            self._write_fd = None
        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None
