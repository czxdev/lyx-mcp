from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import ConflictError, LyXMCPError


@dataclass(frozen=True, slots=True)
class Header:
    tracking_changes: bool
    output_changes: bool
    master: str | None


def authorize(path: str | Path, allowed_roots: tuple[Path, ...]) -> Path:
    target = Path(path).expanduser().resolve(strict=True)
    if target.suffix.lower() != ".lyx" or not target.is_file():
        raise LyXMCPError("INVALID_DOCUMENT", "path must name an existing .lyx file")
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise LyXMCPError("PATH_NOT_ALLOWED", f"document is outside allowed_roots: {target}")
    return target


def header(path: Path) -> Header:
    with path.open("rb") as source:
        data = source.read(2_000_000)
    marker = b"\\end_header"
    if b"\\begin_header" not in data or marker not in data:
        raise LyXMCPError("INVALID_DOCUMENT", "LyX header is absent or too large")
    content = data.split(marker, 1)[0].decode("utf-8", errors="replace")
    fields = {}
    for line in content.splitlines():
        if line.startswith("\\"):
            name, _, value = line.partition(" ")
            fields[name] = value.strip()
    if fields.get("\\tracking_changes") not in ("true", "false"):
        raise LyXMCPError("INVALID_DOCUMENT", "LyX tracking_changes header is missing")
    return Header(
        tracking_changes=fields["\\tracking_changes"] == "true",
        output_changes=fields.get("\\output_changes") == "true",
        master=fields.get("\\master"),
    )


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reject_newer_autosave(path: Path) -> None:
    autosave = path.with_name(f"#{path.name}#")
    if autosave.exists() and autosave.stat().st_mtime_ns > path.stat().st_mtime_ns:
        raise ConflictError("EXTERNAL_UNSAVED_CHANGES_SUSPECTED", f"newer LyX autosave exists: {autosave}")


def assert_unchanged(path: Path, expected_sha256: str) -> None:
    if not path.exists() or fingerprint(path) != expected_sha256:
        raise ConflictError("EXTERNAL_MODIFICATION", f"document changed on disk: {path}")


def snapshot(path: Path, directory: Path) -> Path:
    target = directory / f"{path.name}.{uuid.uuid4().hex}.snapshot"
    shutil.copy2(path, target)
    return target


def restore(path: Path, saved: Path) -> None:
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.lyx-mcp-", dir=path.parent, delete=False) as file:
        staging = Path(file.name)
    try:
        shutil.copy2(saved, staging)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def section_span(data: bytes, start_heading: str, end_heading: str) -> tuple[int, int]:
    if not start_heading or not end_heading or any(character in start_heading + end_heading for character in "\r\n"):
        raise LyXMCPError("INVALID_ARGUMENT", "section headings must be nonempty single-line text")
    lines = data.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def find(heading: str) -> int:
        encoded = heading.encode()
        matches = [
            offsets[index]
            for index in range(len(lines) - 1)
            if lines[index].rstrip(b"\r\n") == b"\\begin_layout Section"
            and lines[index + 1].rstrip(b"\r\n") == encoded
        ]
        if len(matches) != 1:
            raise LyXMCPError("SECTION_NOT_UNIQUE", f"section {heading!r} occurs {len(matches)} times")
        return matches[0]

    start = find(start_heading)
    end = find(end_heading)
    if end <= start:
        raise LyXMCPError("INVALID_ARGUMENT", "end section must follow start section")
    return start, end


def import_tracked_range(
    target: bytes, source: bytes, start_heading: str, end_heading: str
) -> tuple[bytes, int]:
    target_start, target_end = section_span(target, start_heading, end_heading)
    source_start, source_end = section_span(source, start_heading, end_heading)
    fragment = source[source_start:source_end]
    changes = len(re.findall(rb"(?m)^\\change_(?:inserted|deleted) ", fragment))
    if not changes:
        raise LyXMCPError("NO_TRACKED_CHANGES", "source section range has no tracked changes")
    if re.search(rb"(?m)^\\change_(?:inserted|deleted) ", target[target_start:target_end]):
        raise LyXMCPError("TARGET_ALREADY_TRACKED", "target section range already has tracked changes")

    newline = b"\r\n" if b"\r\n" in target else b"\n"
    fragment = fragment.replace(b"\r\n", b"\n").replace(b"\n", newline)
    merged = target[:target_start] + fragment + target[target_end:]

    source_authors = {
        match.group(1): match.group(0).rstrip(b"\r")
        for match in re.finditer(rb"(?m)^\\author (\d+) [^\r\n]+", source)
    }
    target_authors = {
        match.group(1): match.group(0).rstrip(b"\r")
        for match in re.finditer(rb"(?m)^\\author (\d+) [^\r\n]+", target)
    }
    if any(index in target_authors and target_authors[index] != line for index, line in source_authors.items()):
        raise LyXMCPError("AUTHOR_CONFLICT", "source and target use the same author ID differently")
    missing_authors = [line for index, line in source_authors.items() if index not in target_authors]
    if missing_authors:
        end_header = b"\\end_header" + newline
        if merged.count(end_header) != 1:
            raise LyXMCPError("INVALID_DOCUMENT", "target header terminator is missing or ambiguous")
        merged = merged.replace(end_header, newline.join(missing_authors) + newline + end_header, 1)
    return merged, changes


def write_bytes_atomically(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.lyx-mcp-", dir=path.parent, delete=False) as file:
        staging = Path(file.name)
        file.write(data)
    try:
        shutil.copymode(path, staging)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
