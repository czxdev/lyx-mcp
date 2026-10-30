from __future__ import annotations

import difflib
import importlib
import uuid
from pathlib import Path
from typing import Any

from .errors import LyXMCPError
from .index import DocumentIndex


def inspect_pdf(
    options: dict[str, Any],
    allowed_roots: tuple[Path, ...],
    exports: Path,
    index: DocumentIndex,
) -> dict[str, Any]:
    try:
        fitz = importlib.import_module("pymupdf")
    except ModuleNotFoundError as exc:
        raise LyXMCPError("PDF_INTEGRATION_UNAVAILABLE", "install lyx-mcp-server[pdf] for PDF inspection") from exc
    path = Path(options["path"]).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".pdf" or not any(path.is_relative_to(root) for root in (*allowed_roots, exports)):
        raise LyXMCPError("PATH_NOT_ALLOWED", "PDF must be inside allowed_roots or this session's exports")
    action = options.get("action", "annotations")
    if action not in ("annotations", "render"):
        raise LyXMCPError("INVALID_ARGUMENT", "PDF action must be annotations or render")
    with fitz.open(path) as document:
        pages = options.get("pages", [1])
        if not isinstance(pages, list) or not 1 <= len(pages) <= 20:
            raise LyXMCPError("INVALID_ARGUMENT", "request 1..20 explicit one-based pages")
        if any(not isinstance(p, int) or not 1 <= p <= len(document) for p in pages):
            raise LyXMCPError("INVALID_ARGUMENT", "PDF page is outside the document")
        if action == "render":
            dpi = options.get("dpi", 110)
            if not isinstance(dpi, int) or not 36 <= dpi <= 200:
                raise LyXMCPError("INVALID_ARGUMENT", "dpi must be between 36 and 200")
            images = []
            for number in pages:
                output = exports / f"page-{number}-{uuid.uuid4().hex}.png"
                document[number - 1].get_pixmap(dpi=dpi).save(output)
                images.append({"page": number, "path": str(output), "size": output.stat().st_size})
            return {"ok": True, "pages": images, "page_count": len(document)}
        annotations = []
        offset, limit = options.get("offset", 0), options.get("limit", 30)
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise LyXMCPError("INVALID_ARGUMENT", "annotation offset must be nonnegative; limit is 0..100")
        for number in pages:
            page = document[number - 1]
            for annotation in page.annots() or []:
                rect = annotation.rect
                boxes = []
                if annotation.vertices:
                    vertices = annotation.vertices
                    boxes = [
                        fitz.Quad(vertices[i : i + 4]).rect
                        for i in range(0, len(vertices), 4)
                        if len(vertices[i : i + 4]) == 4
                    ]
                selected = " ".join(page.get_textbox(box).strip() for box in boxes).strip()
                context = page.get_textbox(fitz.Rect(rect.x0 - 100, rect.y0 - 40, rect.x1 + 100, rect.y1 + 40))
                matches: list[dict[str, Any]] = []
                query = " ".join(selected.split())
                if query:
                    for paragraph in index.top:
                        text = " ".join(index.text(paragraph).split())
                        score = 1.0 if query in text else difflib.SequenceMatcher(None, query, text).ratio()
                        if score >= 0.5:
                            matches.append({"node_id": paragraph.id, "confidence": round(score, 3)})
                annotations.append(
                    {
                        "page": number,
                        "type": annotation.type[1],
                        "comment": annotation.info.get("content", "")[:2000],
                        "selected_text": selected[:2000],
                        "rect": list(rect),
                        "context": context[:2000],
                        "matches": sorted(matches, key=lambda m: m["confidence"], reverse=True)[:3],
                        "review_required": True,
                    }
                )
        return {
            "ok": True,
            "page_count": len(document),
            "annotations": annotations[offset : offset + limit],
            "total_annotations": len(annotations),
            "next_offset": offset + limit,
            "truncated": offset + limit < len(annotations),
        }
