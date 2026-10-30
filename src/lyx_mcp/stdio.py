from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import cast

import anyio
from mcp.server.stdio import stdio_server

from .server import mcp


class PipeInput:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader

    async def __aiter__(self) -> AsyncIterator[str]:
        while line := await self.reader.readline():
            yield line.decode("utf-8", errors="replace")


class PipeOutput:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        os.set_blocking(fd, False)

    async def write(self, text: str) -> int:
        data = text.encode()
        offset = 0
        loop = asyncio.get_running_loop()
        while offset < len(data):
            try:
                offset += os.write(self.fd, data[offset:])
            except BlockingIOError:
                ready = loop.create_future()
                loop.add_writer(self.fd, ready.set_result, None)
                try:
                    await ready
                finally:
                    loop.remove_writer(self.fd)
        return len(text)

    async def flush(self) -> None:
        return


async def run() -> None:
    incoming, outgoing = os.dup(0), os.dup(1)
    sys.stdout.flush()
    with open(os.devnull, "rb") as null:
        os.dup2(null.fileno(), 0)
    os.dup2(2, 1)
    reader = asyncio.StreamReader(limit=4_000_000)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(incoming, "rb", buffering=0)
    )
    try:
        stdin = cast(anyio.AsyncFile[str], PipeInput(reader))
        stdout = cast(anyio.AsyncFile[str], PipeOutput(outgoing))
        async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
            await mcp._lowlevel_server.run(
                read_stream, write_stream, mcp._lowlevel_server.create_initialization_options()
            )
    finally:
        os.dup2(outgoing, 1)
        transport.close()
        os.close(outgoing)
