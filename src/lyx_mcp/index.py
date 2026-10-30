from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from .errors import LyXMCPError

HEADINGS = {"Part": 0, "Chapter": 1, "Section": 2, "Subsection": 3, "Subsubsection": 4}
SPECIAL_CHARACTERS = {
    "endofsentence": ".",
    "nobreakdash": "-",
    "ldots": "…",
    "softhyphen": "\u00ad",
    "ligaturebreak": "\u200c",
    "allowbreak": "\u200b",
}
INLINE_CHARACTERS = re.compile(
    r"\\(?:SpecialChar (?:" + "|".join(SPECIAL_CHARACTERS) + r")\b|backslash\b|twohyphens\b|threehyphens\b)"
)


@dataclass
class Node:
    id: str
    kind: str
    start: int
    end: int
    content_start: int
    content_end: int
    parent: str | None
    layout: str = ""
    deleted: bool = False
    children: list[Node] = field(default_factory=list)


def token(node_id: str) -> str:
    return "{{" + node_id + "}}"


def encode_text(text: str) -> str:
    if any(c in text for c in "\x00\r\n"):
        raise LyXMCPError("INVALID_TEXT", "use split_paragraph for paragraph breaks; NUL/CR are forbidden")
    return text.replace("\\", "\n\\backslash\n")


class DocumentIndex:
    def __init__(self, source: str) -> None:
        self.source = source
        self.version = hashlib.sha256(source.encode()).hexdigest()
        self.nodes: dict[str, Node] = {}
        self.paragraphs: list[Node] = []
        self.insets: list[Node] = []
        stack: list[Node] = []
        revision_states: list[bool] = []
        offset = 0
        body = False
        deleted = False
        lines = [
            part
            for line in source.splitlines(keepends=True)
            for part in re.split(r"(?=\\begin_inset |\\end_inset)|(?<=\\end_inset)(?=[^\r\n])", line)
            if part
        ]
        for line in lines:
            stripped = line.rstrip("\r\n")
            if stripped == "\\begin_body":
                body = True
            if stripped.startswith("\\change_"):
                deleted = stripped.startswith("\\change_deleted ")
            if body and stripped.startswith(("\\begin_layout ", "\\begin_inset ")):
                paragraph = stripped.startswith("\\begin_layout ")
                value = stripped.split(" ", 1)[1]
                kind = "paragraph" if paragraph else value.split(" ", 1)[0].lower()
                if kind == "commandinset":
                    kind = value.split(" ", 1)[1]
                layout = value if paragraph else ""
                if paragraph and layout.rstrip("*") in HEADINGS:
                    kind = "section"
                number = len(self.nodes)
                node = Node(
                    f"{kind}_{self.version[:12]}_{number}",
                    kind,
                    offset,
                    0,
                    offset + len(line),
                    0,
                    stack[-1].id if stack else None,
                    layout,
                    deleted,
                )
                if stack:
                    stack[-1].children.append(node)
                self.nodes[node.id] = node
                (self.paragraphs if paragraph else self.insets).append(node)
                stack.append(node)
                revision_states.append(deleted)
            elif body and stripped in ("\\end_layout", "\\end_inset"):
                if not stack:
                    raise LyXMCPError("INVALID_DOCUMENT", "unbalanced LyX body terminator")
                node = stack.pop()
                is_layout = node.kind in ("paragraph", "section")
                if (stripped == "\\end_layout") != is_layout:
                    raise LyXMCPError("INVALID_DOCUMENT", "mismatched LyX body terminator")
                node.content_end = offset
                node.end = offset + len(line)
                deleted = revision_states.pop()
            offset += len(line)
        if stack or not body:
            raise LyXMCPError("INVALID_DOCUMENT", "incomplete LyX body")
        self.top = [p for p in self.paragraphs if p.parent is None]
        self._runs: dict[str, list[tuple[str, int, int, str]]] = {}
        self._texts: dict[str, str] = {}
        self._inset_signatures: dict[str, str] = {}

    def active(self, node: Node) -> bool:
        if node.deleted:
            return False
        return self.active(self.nodes[node.parent]) if node.parent else True

    def node(self, node_id: str) -> Node:
        if node_id not in self.nodes:
            raise LyXMCPError("STALE_NODE", f"node {node_id!r} is absent from document version {self.version}")
        return self.nodes[node_id]

    def raw(self, node: Node) -> str:
        return self.source[node.start : node.end]

    def revision_at(self, position: int) -> str:
        parents = [p for p in self.paragraphs if p.content_start <= position <= p.content_end]
        if not parents:
            return "\\change_unchanged"
        paragraph = max(parents, key=lambda p: p.start)
        pieces = []
        cursor = paragraph.content_start
        for child in paragraph.children:
            if child.start >= position:
                break
            pieces.append(self.source[cursor : child.start])
            cursor = min(child.end, position)
        pieces.append(self.source[cursor:position])
        markers = re.findall(r"(?m)^\\change_[^\n]+", "".join(pieces))
        return markers[-1] if markers else "\\change_unchanged"

    def accepted_fragment(self, start: int, end: int) -> str:
        parents = [p for p in self.paragraphs if p.content_start <= start <= end <= p.content_end]
        if not parents:
            raise LyXMCPError("INVALID_SELECTION", "tracked replacements must remain within one paragraph")
        paragraph = max(parents, key=lambda p: p.start)
        result = []
        deleted = self.revision_at(start).startswith("\\change_deleted")

        def append_text(text: str) -> None:
            nonlocal deleted
            for line in text.splitlines(keepends=True):
                if line.startswith("\\change_"):
                    deleted = line.startswith("\\change_deleted")
                elif not deleted:
                    result.append(line)

        cursor = start
        for child in paragraph.children:
            if child.end <= start or child.start >= end:
                continue
            append_text(self.source[cursor : child.start])
            if not deleted and not child.deleted:
                result.append(self.raw(child))
            cursor = child.end
        append_text(self.source[cursor:end])
        return "".join(result)

    def rejected_signature(self) -> list[tuple[str, str]]:
        source = re.sub(
            r"(?m)^\\change_(inserted|deleted) ",
            lambda m: "\\change_" + ("deleted" if m[1] == "inserted" else "inserted") + " ",
            self.source,
        )
        return DocumentIndex(source).signature() if source != self.source else self.signature()

    def runs(self, node: Node) -> list[tuple[str, int, int, str]]:
        if node.id in self._runs:
            return self._runs[node.id]
        result: list[tuple[str, int, int, str]] = []
        children = {n.start: n for n in node.children}
        position = node.content_start
        deleted = node.deleted
        while position < node.content_end:
            if position in children:
                child = children[position]
                if not deleted and not child.deleted:
                    result.append(("inset", position, child.end, token(child.id)))
                position = child.end
                continue
            end = self.source.find("\n", position, node.content_end)
            end = node.content_end if end < 0 else end + 1
            end = min([end] + [p for p in children if position < p < end])
            line = self.source[position:end].rstrip("\r\n")
            if line.startswith("\\change_"):
                deleted = line.startswith("\\change_deleted ")
            elif not deleted and line:
                if line.startswith("\\") and not INLINE_CHARACTERS.match(line):
                    result.append(("markup", position, end, line))
                    position = end
                    continue
                cursor = 0
                for match in INLINE_CHARACTERS.finditer(line):
                    if cursor < match.start():
                        result.append(
                            ("text", position + cursor, position + match.start(), line[cursor : match.start()])
                        )
                    command = match[0][1:]
                    value = (
                        SPECIAL_CHARACTERS[command.split(" ", 1)[1]]
                        if command.startswith("SpecialChar ")
                        else {"backslash": "\\", "twohyphens": "–", "threehyphens": "—"}[command]
                    )
                    result.append(("escape", position + match.start(), position + match.end(), value))
                    cursor = match.end()
                if cursor < len(line):
                    result.append(("text", position + cursor, position + len(line), line[cursor:]))
            position = end
        self._runs[node.id] = result
        return result

    def text(self, node: Node) -> str:
        if node.id in self._texts:
            return self._texts[node.id]
        if node.kind not in ("paragraph", "section"):
            result = "".join(self.text(c) for c in node.children)
        else:
            result = "".join(run[3] for run in self.runs(node) if run[0] != "markup")
        self._texts[node.id] = result
        return result

    def span(
        self,
        node: Node,
        text: str,
        occurrence: int | None = None,
        context_before: str | None = None,
        context_after: str | None = None,
    ) -> tuple[int, int, list[str]]:
        visible = self.text(node)
        matches = list(re.finditer(re.escape(text), visible)) if text else []
        matches = [
            m
            for m in matches
            if (context_before is None or visible[: m.start()].endswith(context_before))
            and (context_after is None or visible[m.end() :].startswith(context_after))
        ]
        selected = occurrence or 1
        if not matches or selected < 1 or selected > len(matches):
            raise LyXMCPError("TARGET_NOT_FOUND", f"text is absent from {node.id}")
        if len(matches) > 1 and occurrence is None:
            raise LyXMCPError(
                "AMBIGUOUS_TARGET", f"{len(matches)} occurrences in {node.id}; select occurrence or context"
            )
        start, end = matches[selected - 1].span()
        return self.source_span(node, start, end)

    def visible_offset(self, node: Node, source_position: int) -> int:
        offset = 0
        for kind, start, end, value in self.runs(node):
            if kind == "markup":
                continue
            if start <= source_position < end:
                return offset + (source_position - start if kind == "text" else 0)
            offset += len(value)
        raise LyXMCPError("INVALID_SELECTION", "source position is outside accepted content")

    def source_span(self, node: Node, start: int, end: int) -> tuple[int, int, list[str]]:
        if not 0 <= start <= end <= len(self.text(node)):
            raise LyXMCPError("INVALID_SELECTION", "selection is outside accepted content")
        cursor = 0
        first: int | None = None
        last = 0
        insets = []
        for kind, raw_start, raw_end, value in self.runs(node):
            if kind == "markup":
                continue
            next_cursor = cursor + len(value)
            if start == end and cursor <= start < next_cursor:
                if kind != "text" and start != cursor:
                    raise LyXMCPError("PARTIAL_INSET", "insert at a complete text/inset boundary")
                point = raw_start + start - cursor if kind == "text" else raw_start
                return point, point, []
            if next_cursor > start and cursor < end:
                if kind != "text" and (cursor < start or end < next_cursor):
                    raise LyXMCPError("PARTIAL_INSET", "select complete inset tokens")
                left = raw_start + max(0, start - cursor) if kind == "text" else raw_start
                right = raw_start + min(len(value), end - cursor) if kind == "text" else raw_end
                if first is None:
                    first = left
                last = right
                if kind == "inset":
                    insets.append(value[2:-2])
            cursor = next_cursor
            if start == end:
                last = raw_end
        if start == end:
            point = last or node.content_start
            return point, point, []
        if first is None:
            raise LyXMCPError("TARGET_NOT_FOUND", "empty source selection")
        return first, last, insets

    def properties(self, node: Node) -> dict[str, str]:
        if node.kind == "formula":
            return {"latex": self.raw(node).split("Formula ", 1)[1].rsplit("\\end_inset", 1)[0].strip()}
        prefix = self.source[node.content_start : node.content_end]
        if node.children:
            prefix = self.source[node.content_start : node.children[0].start]
        return {match[1]: match[2].strip('"') for match in re.finditer(r"(?m)^([A-Za-z_]+) (.+)$", prefix)}

    def inset_signature(self, node: Node) -> str:
        if node.id in self._inset_signatures:
            return self._inset_signatures[node.id]
        pieces = []
        cursor = node.start
        for child in node.children:
            pieces.append(self.source[cursor : child.start])
            if not child.deleted:
                pieces.append(
                    self._paragraph_signature(child)
                    if child.kind in ("paragraph", "section")
                    else self.inset_signature(child)
                )
            cursor = child.end
        pieces.append(self.source[cursor : node.end])
        raw = "\n".join(pieces)
        if node.kind == "formula":
            raw = re.sub(r"([_^])(\\[A-Za-z]+|[^\s{}\\])", r"\1{\2}", raw)
        raw = re.sub(r"(?m)^status (?:open|collapsed)\s*\n", "", raw)
        raw = re.sub(r"(?m)^\\change_[^\n]*\n", "", raw)
        raw = re.sub(r"(?m)^literal \"false\"\s*\n", "", raw)
        result = "\n".join(line.strip() for line in raw.splitlines() if line.strip())
        self._inset_signatures[node.id] = result
        return result

    def _paragraph_signature(self, node: Node) -> str:
        content: list[tuple[str, str]] = []
        for kind, _, _, value in self.runs(node):
            if kind == "inset":
                content.append((kind, self.inset_signature(self.node(value[2:-2]))))
            elif kind == "markup":
                content.append((kind, value))
            elif content and content[-1][0] == "text":
                content[-1] = ("text", content[-1][1] + value)
            else:
                content.append(("text", value))
        return json.dumps([node.layout, content], ensure_ascii=False, separators=(",", ":"))

    def signature(self, nodes: list[Node] | None = None) -> list[tuple[str, str]]:
        result = []
        for paragraph in self.paragraphs if nodes is None else nodes:
            if not self.active(paragraph):
                continue
            visible = self.text(paragraph)
            for inset in paragraph.children:
                visible = visible.replace(token(inset.id), self.inset_signature(inset))
            result.append((paragraph.layout, visible))
        return result

    def revision_signature(self) -> list[tuple[str, list[tuple[str, str, str]]]]:
        authors = {}
        for match in re.finditer(r"(?m)^\\author (\d+) (.+)$", self.source):
            fields = shlex.split(match[2])
            authors[match[1]] = json.dumps((fields + ["", ""])[:2], ensure_ascii=False)
        result = []
        for paragraph in self.paragraphs:
            if not self.active(paragraph):
                continue
            spans: list[tuple[str, str, str]] = []
            for kind, start, _, value in self.runs(paragraph):
                if kind == "markup":
                    continue
                revision = self.revision_at(start)
                marker = revision.split(" ", 2)
                if len(marker) == 3:
                    revision = " ".join((marker[0], authors.get(marker[1], marker[1]), marker[2]))
                if kind == "inset":
                    value = self.inset_signature(self.node(value[2:-2]))
                else:
                    kind = "text"
                if spans and spans[-1][:2] == (revision, kind):
                    spans[-1] = (revision, kind, spans[-1][2] + value)
                else:
                    spans.append((revision, kind, value))
            result.append((paragraph.layout, spans))
        return result

    def outline(self) -> list[dict[str, object]]:
        return [
            {"id": p.id, "title": self.text(p), "layout": p.layout, "level": HEADINGS[p.layout.rstrip("*")]}
            for p in self.top
            if p.kind == "section" and not p.deleted
        ]

    def select(self, selector: dict[str, Any] | None = None, search: str | None = None) -> list[Node]:
        selector = selector or {}
        unknown = set(selector) - {"node_ids", "section", "from_node", "to_before_node", "offset", "limit"}
        if unknown:
            raise LyXMCPError("INVALID_SELECTOR", f"unknown selector fields: {sorted(unknown)}")
        selected = [p for p in self.top if self.active(p)]
        if "node_ids" in selector:
            selected = [self.node(node_id) for node_id in selector["node_ids"]]
        if "section" in selector:
            candidates = [
                p
                for p in self.top
                if p.kind == "section" and (p.id == selector["section"] or self.text(p) == selector["section"])
            ]
            if len(candidates) != 1:
                raise LyXMCPError("SECTION_NOT_UNIQUE", "select one section by ID or exact title")
            section = candidates[0]
            level = HEADINGS[section.layout.rstrip("*")]
            stop = next(
                (
                    p.start
                    for p in self.top
                    if p.start > section.start and p.kind == "section" and HEADINGS[p.layout.rstrip("*")] <= level
                ),
                len(self.source),
            )
            selected = [p for p in selected if section.start <= p.start < stop]
        if "from_node" in selector:
            start = self.node(selector["from_node"]).start
            selected = [p for p in selected if p.start >= start]
        if "to_before_node" in selector:
            end = self.node(selector["to_before_node"]).start
            selected = [p for p in selected if p.start < end]
        if search is not None:
            selected = [
                p
                for p in selected
                if search.casefold() in self.text(p).casefold()
                or any(search.casefold() in self.raw(c).casefold() for c in p.children)
            ]
        offset, limit = selector.get("offset", 0), selector.get("limit", 20)
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 0 <= limit <= 200:
            raise LyXMCPError("INVALID_SELECTOR", "offset must be nonnegative; limit must be between 0 and 200")
        return selected[offset : offset + limit]

    def describe(self, node: Node, representations: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": node.id,
            "type": node.kind,
            "parent": node.parent,
            "layout": node.layout,
            "active": self.active(node),
        }
        if "text" in representations:
            result["text"] = self.text(node)
        if "source" in representations:
            result["source"] = self.raw(node)
        if "nodes" in representations:
            result["nodes"] = [
                {
                    "id": c.id,
                    "type": c.kind,
                    "active": self.active(c),
                    "token": token(c.id),
                    "properties": self.properties(c),
                    "sha256": hashlib.sha256(self.inset_signature(c).encode()).hexdigest(),
                    "paragraph_ids": [p.id for p in c.children if p.kind in ("paragraph", "section")],
                }
                for c in node.children
            ]
        return result
