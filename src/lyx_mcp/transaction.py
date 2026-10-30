from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .document import assert_unchanged, authorize, import_tracked_range, section_span
from .edit import _narrow_replacement
from .errors import LyXMCPError
from .index import DocumentIndex, Node, encode_text
from .revision import normalize_revision_markup


@dataclass
class Patch:
    start: int
    end: int
    replacement: str
    operation_id: str


class TransactionPlan:
    def __init__(self, index: DocumentIndex, allowed_roots: tuple[Path, ...]) -> None:
        self.index = index
        self.allowed_roots = allowed_roots
        self.patches: list[Patch] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.changed_insets: set[str] = set()
        self.author = max([int(n) for n in re.findall(r"(?m)^\\author (\d+) ", index.source)] + [-1]) + 1
        self.timestamp = int(time.time())
        self._new_revisions = False
        self._rebased: set[str] = set()

    def patch(self, start: int, end: int, replacement: str, op_id: str) -> None:
        self.patches.append(Patch(start, end, replacement, op_id))
        self.changed_insets.update(
            n.id for n in self.index.insets if n.start <= start < n.end or start < n.end and end > n.start
        )

    def tracked(self, start: int, end: int, new: str, op_id: str) -> None:
        self._new_revisions = True
        original = self.index.source[start:end]
        old = self.index.accepted_fragment(start, end)
        if old == new:
            raise LyXMCPError("NO_EFFECT", "operation does not change the selection")
        initial = self.index.revision_at(start)
        previous = self.index.revision_at(end)
        if "\\change_" in original or initial.startswith("\\change_inserted"):
            self._rebased.add(op_id)
        if initial.startswith("\\change_inserted") and "\\change_" not in original:
            old = ""
        pieces = []
        if old:
            pieces.append(f"\\change_deleted {self.author} {self.timestamp}\n{old}\n")
        if new:
            pieces.append(f"\\change_inserted {self.author} {self.timestamp}\n{new}\n")
        self.patch(start, end, "\n" + "".join(pieces) + previous + "\n", op_id)

    def paragraph(self, operation: dict[str, Any]) -> Node:
        if operation.get("node_id"):
            node = self.index.node(operation["node_id"])
            if node.kind not in ("paragraph", "section"):
                raise LyXMCPError("WRONG_NODE_TYPE", "operation requires a paragraph node")
            if not self.index.active(node):
                raise LyXMCPError("INACTIVE_NODE", "select an accepted paragraph, not a deleted revision")
            return node
        target = operation.get("old_text", operation.get("anchor", ""))
        candidates = [p for p in self.index.top if self.index.active(p) and target and target in self.index.text(p)]
        if operation.get("context_before") is not None:
            candidates = [p for p in candidates if operation["context_before"] + target in self.index.text(p)]
        if operation.get("context_after") is not None:
            candidates = [p for p in candidates if target + operation["context_after"] in self.index.text(p)]
        if len(candidates) != 1:
            raise LyXMCPError(
                "AMBIGUOUS_TARGET" if candidates else "TARGET_NOT_FOUND", "supply a paragraph node_id from lyx_read"
            )
        return candidates[0]

    def replace(self, operation: dict[str, Any], op_id: str) -> None:
        paragraph = self.paragraph(operation)
        old = operation.get("old_text")
        new = operation.get("new_text", "" if operation["op"] in ("delete", "delete_text") else None)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise LyXMCPError("INVALID_ARGUMENT", "replace_text requires nonempty old_text and new_text")
        start, end, insets = self.index.span(
            paragraph, old, operation.get("occurrence"), operation.get("context_before"), operation.get("context_after")
        )
        preserved = operation.get("preserve_insets", [])
        new_tokens = re.findall(r"\{\{([^{}]+)\}\}", new)
        if insets != preserved or new_tokens != preserved:
            raise LyXMCPError(
                "INSET_PRESERVATION", "list every selected inset in preserve_insets and retain its token order"
            )
        if not insets:
            if old and new and operation["op"] not in ("delete", "delete_text"):
                prefix, stop, narrowed = _narrow_replacement(old, new)
                new = narrowed
                offset = self.index.visible_offset(paragraph, start)
                start, end, _ = self.index.source_span(paragraph, offset + prefix, offset + stop)
            self.tracked(start, end, encode_text(new), op_id)
            return
        old_parts = re.split(r"\{\{[^{}]+\}\}", old)
        new_parts = re.split(r"\{\{[^{}]+\}\}", new)
        boundaries = [start] + [self.index.node(n).end for n in insets]
        endings = [self.index.node(n).start for n in insets] + [end]
        for left, right, before, after in zip(boundaries, endings, old_parts, new_parts, strict=True):
            if before != after:
                self.tracked(left, right, encode_text(after), op_id)

    def properties(self, operation: dict[str, Any], op_id: str) -> None:
        node = self.index.node(operation["node_id"])
        if not self.index.active(node):
            raise LyXMCPError("INACTIVE_NODE", "select an accepted inset, not a deleted revision")
        allowed = {
            "citation": {"key", "LatexCommand", "before", "after", "literal"},
            "ref": {"reference", "LatexCommand", "name", "plural", "caps", "noprefix"},
        }
        if node.kind not in allowed:
            raise LyXMCPError("WRONG_NODE_TYPE", "property updates require a citation or ref node")
        props = dict(operation.get("properties", {}))
        if operation["op"] == "set_citation":
            keys = operation.get("keys", [])
            if isinstance(keys, str):
                keys = keys.split(",")
            if not keys or any(not re.fullmatch(r'[^\s"|{}\\,]+', key) for key in keys):
                raise LyXMCPError("INVALID_ARGUMENT", "keys must contain valid bibliography keys")
            props["key"] = ",".join(keys)
        if not props or set(props) - allowed[node.kind]:
            raise LyXMCPError("INVALID_ARGUMENT", "unsupported or empty inset property update")
        raw = self.index.raw(node)
        for key, value in props.items():
            if not isinstance(value, str) or any(c in value for c in '\n\r\x00"\\'):
                raise LyXMCPError("INVALID_ARGUMENT", "property values must be single-line quoted strings")
            line = f"{key} {value}" if key == "LatexCommand" else f'{key} "{value}"'
            pattern = rf"(?m)^{re.escape(key)} [^\n]*"
            raw = (
                re.sub(pattern, line, raw)
                if re.search(pattern, raw)
                else raw.replace("\\end_inset", line + "\n\\end_inset", 1)
            )
        self.tracked(node.start, node.end, raw, op_id)
        self.changed_insets.add(node.id)

    def operation(self, operation: dict[str, Any], op_id: str) -> None:
        op = operation.get("op")
        visible = True
        details: dict[str, Any] = {}
        if op in ("replace_text", "replace", "delete_text", "delete", "replace_selection"):
            self.replace(operation, op_id)
        elif op in ("insert_before", "insert_after", "insert_citation"):
            node = self.paragraph(operation)
            start, end, _ = self.index.span(
                node,
                operation.get("anchor", ""),
                operation.get("occurrence"),
                operation.get("context_before"),
                operation.get("context_after"),
            )
            position = operation.get("position", "after") if op == "insert_citation" else op.removeprefix("insert_")
            if position not in ("before", "after"):
                raise LyXMCPError("INVALID_ARGUMENT", "position must be before or after")
            point = start if position == "before" else end
            if op == "insert_citation":
                keys = operation.get("keys", [])
                if isinstance(keys, str):
                    keys = keys.split(",")
                if not keys or any(not re.fullmatch(r'[^\s"|{}\\,]+', key) for key in keys):
                    raise LyXMCPError("INVALID_ARGUMENT", "invalid citation keys")
                raw = '\\begin_inset CommandInset citation\nLatexCommand cite\nkey "' + ",".join(keys)
                raw += '"\nliteral "false"\n\n\\end_inset\n'
            else:
                raw = encode_text(operation["text"])
            self.tracked(point, point, raw, op_id)
        elif op in ("set_citation", "set_reference", "set_inset_properties"):
            self.properties(operation, op_id)
        elif op == "edit_formula":
            node = self.index.node(operation["node_id"])
            if not self.index.active(node):
                raise LyXMCPError("INACTIVE_NODE", "select an accepted formula, not a deleted revision")
            if node.kind != "formula":
                raise LyXMCPError("WRONG_NODE_TYPE", "edit_formula requires a formula node")
            latex = operation["latex"]
            if not isinstance(latex, str) or any(c in latex for c in ("\x00", "\\begin_inset", "\\end_inset")):
                raise LyXMCPError("INVALID_ARGUMENT", "invalid formula content")
            self.tracked(node.start, node.end, "\\begin_inset Formula " + latex + "\n\\end_inset\n", op_id)
            self.changed_insets.add(node.id)
        elif op == "split_paragraph":
            node = self.paragraph(operation)
            _, end, _ = self.index.span(node, operation["after_text"], operation.get("occurrence"))
            right = end
            while self.index.source[right : right + 1] in (" ", "\t"):
                right += 1
            revision = self.index.revision_at(right)
            self.patch(end, right, f"\n\\end_layout\n\n\\begin_layout {node.layout}\n{revision}\n", op_id)
            visible = False
        elif op == "join_paragraphs":
            first = self.paragraph(operation)
            second = self.index.node(operation["next_node_id"])
            siblings = [n for n in self.index.paragraphs if n.parent == first.parent]
            if siblings.index(first) + 1 >= len(siblings) or siblings[siblings.index(first) + 1] != second:
                raise LyXMCPError("INVALID_STRUCTURE", "join requires adjacent sibling paragraphs")
            start = first.content_end
            while start > first.content_start and self.index.source[start - 1] in "\r\n":
                start -= 1
            self.patch(start, second.content_start, " \n\\change_unchanged\n", op_id)
            visible = False
        elif op == "set_layout":
            node = self.paragraph(operation)
            layout = operation["layout"]
            if not re.fullmatch(r"[\w *-]+", layout):
                raise LyXMCPError("INVALID_ARGUMENT", "invalid paragraph layout")
            self.patch(node.start, node.content_start, f"\\begin_layout {layout}\n", op_id)
            visible = False
        elif op == "move_passage":
            first = self.index.node(operation["from_node"])
            stop = self.index.node(operation["to_before_node"])
            destination = self.index.node(operation["before_node"])
            if not (first.parent == stop.parent == destination.parent) or not first.start < stop.start:
                raise LyXMCPError("INVALID_STRUCTURE", "move requires an ordered sibling range")
            if first.start <= destination.start <= stop.start:
                raise LyXMCPError("INVALID_STRUCTURE", "destination overlaps the moved passage")
            self.patch(first.start, stop.start, "", op_id)
            self.patch(destination.start, destination.start, self.index.source[first.start : stop.start], op_id)
            visible = False
        elif op in ("edit_table_cell", "set_caption"):
            node = self.index.node(operation["node_id"])
            if not self.index.active(node):
                raise LyXMCPError("INACTIVE_NODE", "select an accepted inset, not a deleted revision")
            candidates = [p for p in self.index.paragraphs if node.start < p.start < node.end]
            if op == "edit_table_cell":
                if node.kind != "tabular":
                    raise LyXMCPError("WRONG_NODE_TYPE", "edit_table_cell requires a tabular node")
                rows = list(re.finditer(r"<row\b[^>]*>(.*?)</row>", self.index.raw(node), re.S))
                row = rows[operation["row"]]
                cells = list(re.finditer(r"<cell\b[^>]*>(.*?)</cell>", row[1], re.S))
                cell = cells[operation["column"]]
                start = node.start + row.start(1) + cell.start(1)
                end = node.start + row.start(1) + cell.end(1)
                candidates = [p for p in candidates if start <= p.start < end]
            elif node.kind != "caption":
                raise LyXMCPError("WRONG_NODE_TYPE", "set_caption requires a caption node")
            if len(candidates) != 1:
                raise LyXMCPError("INVALID_STRUCTURE", "select a single paragraph in the cell/caption instead")
            paragraph = candidates[0]
            self.replace(
                {
                    "op": "replace_text",
                    "node_id": paragraph.id,
                    "old_text": operation.get("old_text", self.index.text(paragraph)),
                    "new_text": operation["new_text"],
                    "preserve_insets": operation.get("preserve_insets", []),
                },
                op_id,
            )
            self.changed_insets.add(node.id)
        elif op in ("import_revision_range", "normalize_revisions"):
            if op == "import_revision_range":
                source = authorize(operation["source_path"], self.allowed_roots)
                assert_unchanged(source, operation["source_sha256"])
                start, end = section_span(
                    self.index.source.encode(), operation["start_heading"], operation["end_heading"]
                )
                start = len(self.index.source.encode()[:start].decode())
                end = len(self.index.source.encode()[:end].decode())
                self.changed_insets.update(n.id for n in self.index.insets if start <= n.start < end)
                revised, imported_changes = import_tracked_range(
                    self.index.source.encode(),
                    source.read_bytes(),
                    operation["start_heading"],
                    operation["end_heading"],
                )
                details["imported_change_markers"] = imported_changes
            else:
                revised, merged_groups, accepted_ert = normalize_revision_markup(
                    self.index.source.encode(), operation["author_id"], operation["timestamp"]
                )
                details = {"merged_groups": merged_groups, "accepted_structural_ert_replacements": accepted_ert}
                visible = not bool(accepted_ert)
            changed = revised.decode()
            before_lines = self.index.source.splitlines(keepends=True)
            after_lines = changed.splitlines(keepends=True)
            old_offsets, new_offsets = [0], [0]
            for value in before_lines:
                old_offsets.append(old_offsets[-1] + len(value))
            for value in after_lines:
                new_offsets.append(new_offsets[-1] + len(value))
            for tag, i, j, a, b in difflib.SequenceMatcher(
                None, before_lines, after_lines, autojunk=False
            ).get_opcodes():
                if tag != "equal":
                    self.patch(old_offsets[i], old_offsets[j], changed[new_offsets[a] : new_offsets[b]], op_id)
            self.changed_insets.update(n.id for n in self.index.insets if self.index.raw(n) not in changed)
        else:
            raise LyXMCPError("UNKNOWN_OPERATION", f"unsupported operation: {op!r}")
        self.results[op_id] = {"ok": True, "op": op, "visibly_tracked": visible, **details}
        if op_id in self._rebased:
            self.results[op_id]["rebased_prior_revisions"] = True
        if not visible:
            self.results[op_id]["tracking_note"] = (
                "LyX does not visibly track this structural change; text revisions are retained"
            )

    def preflight(
        self,
        operations: list[dict[str, Any]],
        protect: list[dict[str, str]],
        assertions: dict[str, Any],
    ) -> list[dict[str, str]]:
        errors = []
        seen = set()
        if not operations:
            return [{"id": "", "code": "INVALID_ARGUMENT", "diagnostic": "operations must not be empty"}]
        for operation in operations:
            if operation.get("op") != "import_revision_range":
                continue
            try:
                source = authorize(operation["source_path"], self.allowed_roots).read_text()
                authors = [int(n) for n in re.findall(r"(?m)^\\author (\d+) ", source)]
                self.author = max([self.author - 1, *authors]) + 1
            except (LyXMCPError, OSError, KeyError, TypeError):
                continue
        for number, operation in enumerate(operations):
            op_id = operation.get("id", "")
            try:
                if not isinstance(op_id, str) or not op_id or op_id in seen:
                    raise LyXMCPError("INVALID_OPERATION_ID", "each operation needs a unique nonempty id")
                seen.add(op_id)
                self.operation(operation, op_id)
            except (LyXMCPError, KeyError, IndexError, TypeError, ValueError) as exc:
                errors.append(
                    {
                        "id": str(op_id or number),
                        "code": exc.code if isinstance(exc, LyXMCPError) else "INVALID_ARGUMENT",
                        "diagnostic": str(exc),
                    }
                )
        ordered = sorted(self.patches, key=lambda p: (p.start, p.end))
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.start < left.end or right.start == left.start:
                errors.append(
                    {
                        "id": right.operation_id,
                        "code": "OVERLAPPING_OPERATIONS",
                        "diagnostic": f"overlaps {left.operation_id}; all targets use the same baseline",
                    }
                )
        ranges = []
        try:
            for selection in protect:
                first = self.index.node(selection["from_node"])
                last = self.index.node(selection["to_before_node"])
                if last.start <= first.start:
                    raise LyXMCPError("INVALID_RANGE", "protected range must be ordered")
                ranges.append((first.start, last.start))
            for node_id in assertions.get("unchanged_insets", []):
                node = self.index.node(node_id)
                ranges.append((node.start, node.end))
            allowed = [
                (self.index.node(s["from_node"]).start, self.index.node(s["to_before_node"]).start)
                for s in assertions.get("only_regions", [])
            ]
            for patch in ordered:
                if any(patch.start < end and patch.end > start or start <= patch.start < end for start, end in ranges):
                    errors.append(
                        {
                            "id": patch.operation_id,
                            "code": "PROTECTED_RANGE",
                            "diagnostic": "operation modifies protected content",
                        }
                    )
                if allowed and not any(start <= patch.start <= patch.end <= end for start, end in allowed):
                    errors.append(
                        {
                            "id": patch.operation_id,
                            "code": "OUTSIDE_SELECTED_REGIONS",
                            "diagnostic": "operation is outside allowed regions",
                        }
                    )
        except (LyXMCPError, KeyError, TypeError) as exc:
            errors.append({"id": "assertions", "code": "INVALID_ASSERTION", "diagnostic": str(exc)})
        return errors

    def render(self) -> str:
        source = self.index.source
        for patch in sorted(self.patches, key=lambda p: p.start, reverse=True):
            source = source[: patch.start] + patch.replacement + source[patch.end :]
        if self._new_revisions:
            author = f'\\author {self.author} "LyX MCP" ""\n'
            source = source.replace("\\end_header", author + "\\end_header", 1)
        source = re.sub(r"(?m)^\\tracking_changes (?:true|false)", r"\\tracking_changes true", source, count=1)
        source = re.sub(r"(?m)^\\output_changes (?:true|false)", r"\\output_changes true", source, count=1)
        return source
