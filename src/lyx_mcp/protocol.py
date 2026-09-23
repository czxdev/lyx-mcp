from __future__ import annotations

from dataclasses import dataclass

from .errors import LyXMCPError


@dataclass(frozen=True, slots=True)
class Response:
    kind: str
    client: str
    function: str
    payload: str


def _single_line(value: str, label: str) -> None:
    if any(char in value for char in "\r\n\x00"):
        raise LyXMCPError("INVALID_ARGUMENT", f"{label} contains a line break or NUL")


def command(client: str, function: str, argument: str = "") -> bytes:
    _single_line(client, "client")
    _single_line(function, "function")
    _single_line(argument, "argument")
    if not client or ":" in client or not function or ":" in function:
        raise LyXMCPError("INVALID_ARGUMENT", "invalid LyXServer client or function")
    return f"LYXCMD:{client}:{function}:{argument}\n".encode()


def hello(client: str) -> bytes:
    _single_line(client, "client")
    if not client or ":" in client:
        raise LyXMCPError("INVALID_ARGUMENT", "invalid LyXServer client")
    return f"LYXSRV:{client}:hello\n".encode("ascii")


def bye(client: str) -> bytes:
    _single_line(client, "client")
    if not client or ":" in client:
        raise LyXMCPError("INVALID_ARGUMENT", "invalid LyXServer client")
    return f"LYXSRV:{client}:bye\n".encode("ascii")


def parse(line: bytes) -> Response:
    try:
        parts = line.decode("utf-8").rstrip("\r\n").split(":", 3)
    except UnicodeDecodeError as exc:
        raise LyXMCPError("PROTOCOL_ERROR", "invalid LyXServer UTF-8 response") from exc
    if len(parts) == 3 and parts[0] == "LYXSRV":
        return Response("LYXSRV", parts[1], parts[2], "")
    if len(parts) == 4 and parts[0] in ("INFO", "ERROR"):
        return Response(parts[0], parts[1], parts[2], parts[3])
    if len(parts) >= 2 and parts[0] == "NOTIFY":
        return Response("NOTIFY", "", "", ":".join(parts[1:]))
    raise LyXMCPError("PROTOCOL_ERROR", f"unexpected LyXServer response: {line!r}")
