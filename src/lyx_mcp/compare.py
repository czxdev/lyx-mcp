from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from typing import Any

from .index import DocumentIndex, Node, token


def _inset_token(index: DocumentIndex, inset: Node) -> str:
    digest = hashlib.sha256(index.inset_signature(inset).encode()).hexdigest()
    return "{{inset:" + inset.kind + ":" + digest + "}}"


def _text(index: DocumentIndex, paragraph: Node) -> str:
    text = index.text(paragraph)
    for inset in paragraph.children:
        text = text.replace(token(inset.id), _inset_token(index, inset))
    return text


def _visible_offset(index: DocumentIndex, paragraph: Node, offset: int) -> int:
    shift = 0
    for match in re.finditer(r"\{\{([^{}]+)\}\}", index.text(paragraph)):
        inset = index.nodes.get(match[1])
        if inset is None:
            continue
        position = match.start() + shift
        if offset <= position:
            break
        shift += len(_inset_token(index, inset)) - len(match[0])
    return offset - shift


def _anchor(text: str, position: int) -> tuple[str, str]:
    left, right = max(0, position - 40), min(len(text), position + 40)
    for match in re.finditer(r"\{\{[^{}]+\}\}", text):
        if match.start() < left < match.end():
            left = match.start()
        if match.start() < right < match.end():
            right = match.end()
    return (text[left:position], "after") if position else (text[position:right], "before")


def _alignment(before: list[str], after: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    for tag, i, j, a, b in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == "equal" or tag == "replace" and j - i == b - a:
            result.update(zip(range(i, j), range(a, b), strict=True))
    return result


def _map_span(
    before: str,
    current: str,
    start: int,
    end: int,
    blocks: list[difflib.Match] | None = None,
) -> tuple[int, int] | None:
    if before == current:
        return start, end
    if blocks is None:
        blocks = difflib.SequenceMatcher(None, before, current, autojunk=False).get_matching_blocks()
    for block in blocks:
        if block.a <= start <= end <= block.a + block.size:
            left, right = max(0, start - 12), min(len(before), end + 12)
            mapped = block.b + start - block.a
            if current[max(0, mapped - (start - left)) : mapped + right - start] != before[left:right]:
                continue
            if start == end and before != current:
                left, right = max(0, start - 12), min(len(before), end + 12)
                mapped = block.b + start - block.a
                if current[max(0, mapped - (start - left)) : mapped + right - end] != before[left:right]:
                    continue
            return block.b + start - block.a, block.b + end - block.a
    return None


def compare_documents(base: DocumentIndex, revised: DocumentIndex, current: DocumentIndex) -> dict[str, Any]:
    old = [_text(base, p) for p in base.top]
    previous = [_text(revised, p) for p in revised.top]
    now = [_text(current, p) for p in current.top]
    old_to_new = _alignment(old, previous)
    old_to_current = _alignment(old, now)
    hunks: list[dict[str, Any]] = []
    for number, before in enumerate(old):
        if number not in old_to_new:
            hunks.append(
                {
                    "id": f"h{len(hunks)}",
                    "conflict": True,
                    "reason": "paragraph deletion or restructuring requires review",
                    "base_node": base.top[number].id,
                    "before": before,
                    "after": "",
                }
            )
            continue
        previous_number = old_to_new[number]
        after = previous[previous_number]
        if base.top[number].layout != revised.top[previous_number].layout:
            hunks.append(
                {
                    "id": f"h{len(hunks)}",
                    "conflict": True,
                    "reason": "paragraph layout change requires an explicit set_layout operation",
                    "before": base.top[number].layout,
                    "after": revised.top[previous_number].layout,
                }
            )
        if before == after:
            continue
        current_number = old_to_current.get(number)
        current_node = current.top[current_number] if current_number is not None else None
        current_text = now[current_number] if current_number is not None else ""
        matching = (
            difflib.SequenceMatcher(None, before, current_text, autojunk=False).get_matching_blocks()
            if current_node and before != current_text
            else None
        )
        before_words = re.findall(r"\{\{inset:.*?\}\}|\w+|[^\w]", before, re.S)
        after_words = re.findall(r"\{\{inset:.*?\}\}|\w+|[^\w]", after, re.S)
        for tag, i, j, a, b in difflib.SequenceMatcher(None, before_words, after_words, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            start = len("".join(before_words[:i]))
            end = len("".join(before_words[:j]))
            old_text, new_text = "".join(before_words[i:j]), "".join(after_words[a:b])
            mapped = _map_span(before, current_text, start, end, matching) if current_node else None
            conflict = mapped is None or "{{inset:" in old_text + new_text
            hunk: dict[str, Any] = {
                "id": f"h{len(hunks)}",
                "base_node": base.top[number].id,
                "current_node": current_node.id if current_node else None,
                "before": old_text,
                "after": new_text,
                "context_before": before[max(0, start - 80) : start],
                "context_after": before[end : end + 80],
                "conflict": conflict,
            }
            if conflict:
                hunk["reason"] = (
                    "current author changed this span, or inset changes require an explicit inset operation"
                )
            elif current_node and mapped:
                left, right = (_visible_offset(current, current_node, point) for point in mapped)
                visible = current.text(current_node)
                if old_text:
                    operation: dict[str, Any] = {
                        "op": "replace_text",
                        "node_id": current_node.id,
                        "old_text": visible[left:right],
                        "new_text": new_text,
                        "context_before": visible[max(0, left - 80) : left],
                        "context_after": visible[right : right + 80],
                    }
                else:
                    anchor, position = _anchor(visible, left)
                    operation = {
                        "op": "insert_" + position,
                        "node_id": current_node.id,
                        "anchor": anchor,
                        "text": new_text,
                    }
                operation["id"] = hunk["id"]
                hunk["operation"] = operation
            hunks.append(hunk)
    covered = set(old_to_new.values())
    for number, paragraph in enumerate(revised.top):
        if number not in covered:
            hunks.append(
                {
                    "id": f"h{len(hunks)}",
                    "conflict": True,
                    "reason": "new paragraph requires an explicit structural operation",
                    "before": "",
                    "after": revised.text(paragraph),
                }
            )
    return {
        "compare_id": uuid.uuid4().hex,
        "base_sha256": base.version,
        "revised_sha256": revised.version,
        "expected_sha256": current.version,
        "hunks": hunks,
        "conflicts": sum(bool(h["conflict"]) for h in hunks),
    }
