from __future__ import annotations

import re

from .errors import LyXMCPError


def _pair_pattern(author_id: int, timestamp: int) -> re.Pattern[bytes]:
    marker = f"{author_id} {timestamp}".encode()
    return re.compile(
        rb"(?m)^\\change_deleted " + marker + rb"\r?\n"
        rb"(?P<old>(?:(?!^\\).)*?)"
        rb"^\\change_inserted " + marker + rb"\r?\n"
        rb"(?P<new>(?:(?!^\\).)*?)"
        rb"^\\change_unchanged\r?\n",
        re.S,
    )


def _ert_pattern(author_id: int, timestamp: int) -> re.Pattern[bytes]:
    marker = f"{author_id} {timestamp}".encode()
    return re.compile(
        rb"(?m)^\\change_deleted " + marker + rb"\r?\n"
        rb"(?P<old>\\begin_inset ERT\r?\n.*?^\\end_inset\r?\n)\s*"
        rb"^\\change_inserted " + marker + rb"\r?\n\s*"
        rb"(?P<new>\\begin_inset ERT\r?\n.*?^\\end_inset\r?\n)\s*"
        rb"^\\change_unchanged\r?\n",
        re.S,
    )


def _boundary(pair: re.Match[bytes]) -> bool:
    old = pair.group("old").rstrip()
    new = pair.group("new").rstrip()
    return old.endswith((b".", b"?", b"!")) and new.endswith((b".", b"?", b"!"))


def _short_gap(gap: bytes) -> bool:
    if b"\\" in gap or len(gap) > 40 or re.search(rb"[.!?;:]", gap):
        return False
    return len(re.findall(rb"[A-Za-z0-9]+", gap)) <= 2


def normalize_revision_markup(data: bytes, author_id: int, timestamp: int) -> tuple[bytes, int, int]:
    if author_id < 0 or timestamp < 0:
        raise LyXMCPError("INVALID_ARGUMENT", "author_id and timestamp must be nonnegative")
    ert_matches = [
        match
        for match in _ert_pattern(author_id, timestamp).finditer(data)
        if b"resizebox{" in match.group("old")
        and b"resizebox{" in match.group("new")
        and match.group("old").count(b"}{!}{") == 1
        and match.group("new").count(b"}{!}{") == 1
    ]
    for match in reversed(ert_matches):
        data = data[: match.start()] + match.group("new") + data[match.end() :]

    matches = list(_pair_pattern(author_id, timestamp).finditer(data))
    groups: list[list[re.Match[bytes]]] = []
    active: list[re.Match[bytes]] = []
    for match in matches:
        if active:
            gap = data[active[-1].end() : match.start()]
            if not _short_gap(gap) or _boundary(active[-1]):
                if len(active) > 1:
                    groups.append(active)
                active = []
        active.append(match)
    if len(active) > 1:
        groups.append(active)

    newline = b"\r\n" if b"\r\n" in data else b"\n"
    marker = f"{author_id} {timestamp}".encode()
    for group in reversed(groups):
        old = group[0].group("old")
        new = group[0].group("new")
        for previous, current in zip(group, group[1:], strict=False):
            gap = data[previous.end() : current.start()]
            old += gap + current.group("old")
            new += gap + current.group("new")
        replacement = (
            b"\\change_deleted " + marker + newline + old
            + b"\\change_inserted " + marker + newline + new
            + b"\\change_unchanged" + newline
        )
        data = data[: group[0].start()] + replacement + data[group[-1].end() :]
    return data, len(groups), len(ert_matches)
