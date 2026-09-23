from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .client import LyXClient
from .errors import LyXMCPError


class EditSpec(BaseModel):
    op: Literal["replace", "delete", "insert_before", "insert_after"]
    old_text: str | None = None
    new_text: str | None = None
    anchor: str | None = None
    text: str | None = None
    occurrence: int = Field(default=1, ge=1)
    require_unique: bool = True
    context_before: str | None = None
    context_after: str | None = None

    @model_validator(mode="after")
    def valid_fields(self) -> EditSpec:
        target = self.anchor if self.op.startswith("insert_") else self.old_text
        if not target or len(target.encode()) > 2000 or any(character in target for character in "\r\n\x00"):
            raise ValueError("target must be nonempty single-line text of at most 2000 bytes")
        if self.op == "replace" and self.new_text is None:
            raise ValueError("replace requires new_text")
        if self.op.startswith("insert_") and not self.text:
            raise ValueError("insert requires nonempty text")
        replacement = self.text if self.op.startswith("insert_") else self.new_text
        if replacement and ("\x00" in replacement or "\r" in replacement.replace("\r\n", "")):
            raise ValueError("replacement contains NUL or lone CR")
        return self


@dataclass(frozen=True, slots=True)
class PlannedEdit:
    index: int
    spec: EditSpec
    start: int
    end: int
    insertion_point: int
    search_occurrence: int
    replacement: str
    target: str


def _narrow_replacement(old: str, new: str) -> tuple[int, int, str]:
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    if prefix == len(old) and prefix == len(new):
        raise LyXMCPError("NO_EFFECT", "replacement text is identical to the target")
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    old_end = len(old) - suffix
    new_end = len(new) - suffix
    if prefix == old_end:
        if old_end < len(old) and old[old_end].isalnum():
            while old_end < len(old) and old[old_end].isalnum():
                old_end += 1
                new_end += 1
        elif prefix:
            while prefix and old[prefix - 1].isalnum():
                prefix -= 1
            if prefix == old_end:
                prefix -= 1
    if prefix < len(old) and prefix and old[prefix - 1].isalnum() and old[prefix].isalnum():
        while prefix and old[prefix - 1].isalnum():
            prefix -= 1
    if old_end < len(old) and old_end and old[old_end - 1].isalnum() and old[old_end].isalnum():
        while old_end < len(old) and old[old_end].isalnum():
            old_end += 1
            new_end += 1
    if "-" in old[prefix:old_end] and not any(character.isalnum() for character in old[prefix:old_end]):
        while prefix and not old[prefix - 1].isspace():
            prefix -= 1
        while old_end < len(old) and not old[old_end].isspace():
            old_end += 1
            new_end += 1
    return prefix, old_end, new[prefix:new_end]


def plan(baseline: str, edits: list[EditSpec]) -> tuple[list[PlannedEdit], str]:
    if not edits:
        raise LyXMCPError("INVALID_ARGUMENT", "edits must not be empty")
    planned = []
    for index, spec in enumerate(edits):
        target = spec.anchor if spec.op.startswith("insert_") else spec.old_text
        assert target is not None
        matches = list(re.finditer(re.escape(target), baseline, flags=re.IGNORECASE))
        exact = [
            match
            for match in matches
            if match.group() == target
            and (spec.context_before is None or baseline[: match.start()].endswith(spec.context_before))
            and (spec.context_after is None or baseline[match.end() :].startswith(spec.context_after))
        ]
        if not exact:
            raise LyXMCPError("TARGET_NOT_FOUND", f"edit {index}: target not found")
        if spec.require_unique and len(exact) != 1:
            raise LyXMCPError("AMBIGUOUS_TARGET", f"edit {index}: {len(exact)} matches")
        if spec.occurrence > len(exact):
            raise LyXMCPError("TARGET_NOT_FOUND", f"edit {index}: occurrence exceeds match count")
        selected = exact[spec.occurrence - 1]
        search_occurrence = next(
            position for position, match in enumerate(matches, 1) if match.start() == selected.start()
        )
        replacement = spec.text if spec.op.startswith("insert_") else spec.new_text
        if spec.op == "delete":
            replacement = ""
        assert replacement is not None
        if spec.op == "replace":
            prefix, old_end, replacement = _narrow_replacement(target, replacement)
            selected_start = selected.start() + prefix
            selected_end = selected.start() + old_end
            target = target[prefix:old_end]
            search_occurrence = next(
                position
                for position, match in enumerate(re.finditer(re.escape(target), baseline, flags=re.IGNORECASE), 1)
                if match.start() == selected_start
            )
        else:
            selected_start = selected.start()
            selected_end = selected.end()
        insertion_point = selected.end() if spec.op == "insert_after" else selected.start()
        if spec.op in ("replace", "delete"):
            insertion_point = selected_start
        boundary = selected_end if spec.op in ("replace", "delete") else insertion_point
        if "\n" in replacement and baseline[boundary : boundary + 1] != "\n":
            raise LyXMCPError("UNSAFE_MULTILINE_EDIT", "multiline text must end at a paragraph boundary")
        planned.append(
            PlannedEdit(
                index,
                spec,
                selected_start,
                selected_end,
                insertion_point,
                search_occurrence,
                replacement,
                target,
            )
        )
    ordered = sorted(planned, key=lambda item: item.insertion_point, reverse=True)
    for left, right in zip(ordered, ordered[1:], strict=False):
        if right.end > left.start or right.insertion_point == left.insertion_point:
            raise LyXMCPError("OVERLAPPING_EDITS", "edits overlap or share an insertion point")
    expected = baseline
    for item in ordered:
        visible_replacement = item.replacement.replace("\r\n", "\n").replace("\n", "\n\n")
        if item.spec.op.startswith("insert_"):
            expected = expected[: item.insertion_point] + visible_replacement + expected[item.insertion_point :]
        else:
            expected = expected[: item.start] + visible_replacement + expected[item.end :]
    return ordered, expected


def validate_source_spans(source: str, baseline: str, edits: list[PlannedEdit]) -> None:
    flattened = source.replace("\r", "").replace("\n", "")
    for item in edits:
        target = item.target
        if flattened.count(target) != baseline.count(target):
            raise LyXMCPError(
                "UNSUPPORTED_SPAN",
                f"edit {item.index}: target cannot be mapped uniquely to contiguous plain text in the LyX source",
            )


async def _insert(client: LyXClient, text: str) -> None:
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized or "\x00" in normalized:
        raise LyXMCPError("INVALID_ARGUMENT", "text contains CR or NUL")
    for index, segment in enumerate(normalized.split("\n")):
        if index:
            await client.call("paragraph-break")
        for position in range(0, len(segment), 500):
            await client.call("self-insert", segment[position : position + 500])


async def apply(client: LyXClient, edits: list[PlannedEdit]) -> None:
    for item in edits:
        await client.call("buffer-begin")
        for _ in range(item.search_occurrence):
            response = await client.call("word-find-forward", item.target)
            if response:
                raise LyXMCPError("TARGET_NOT_FOUND", f"LyX search: {response}")
        if item.spec.op == "delete":
            await client.call("cut")
        elif item.spec.op == "replace":
            if item.replacement.startswith("\n") or not item.replacement:
                await client.call("cut")
            await _insert(client, item.replacement)
        else:
            await client.call("mark-off")
            if item.spec.op == "insert_before":
                for _ in item.target:
                    await client.call("char-backward")
            await _insert(client, item.replacement)
