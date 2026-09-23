from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from .config import load_config
from .edit import EditSpec
from .service import LyXService


@dataclass
class AppContext:
    service: LyXService


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    service = LyXService(load_config())
    await service.start()
    try:
        yield AppContext(service)
    finally:
        await service.close()


mcp = MCPServer("lyx-mcp", lifespan=lifespan)


@mcp.tool()
async def lyx_get_document_state(path: str, ctx: Context[AppContext]) -> dict[str, object]:
    """Read a LyX document's checksum and Track Changes state."""
    return await ctx.request_context.lifespan_context.service.get_document_state(path)


@mcp.tool()
async def lyx_read(path: str, ctx: Context[AppContext], format: Literal["text", "latex"] = "text") -> dict[str, str]:
    """Read text or LaTeX exported by an isolated LyX process."""
    return await ctx.request_context.lifespan_context.service.read(path, format)


@mcp.tool()
async def lyx_apply_edits(
    path: str,
    edits: list[EditSpec],
    ctx: Context[AppContext],
    master_path: str | None = None,
    compile: bool = True,
    checker_profile: str | None = None,
) -> dict[str, object]:
    """Apply a batch of tracked text edits and roll back if verification fails."""
    return await ctx.request_context.lifespan_context.service.apply_edits(
        path, edits, master_path, compile, checker_profile
    )


@mcp.tool()
async def lyx_replace_text(
    path: str,
    old_text: str,
    new_text: str,
    ctx: Context[AppContext],
    master_path: str | None = None,
    compile: bool = True,
) -> dict[str, object]:
    """Replace one uniquely identified text span with Track Changes enabled."""
    edit = EditSpec(op="replace", old_text=old_text, new_text=new_text)
    return await ctx.request_context.lifespan_context.service.apply_edits(path, [edit], master_path, compile)


@mcp.tool()
async def lyx_delete_text(
    path: str, old_text: str, ctx: Context[AppContext], compile: bool = True
) -> dict[str, object]:
    """Delete one uniquely identified text span with Track Changes enabled."""
    edit = EditSpec(op="delete", old_text=old_text)
    return await ctx.request_context.lifespan_context.service.apply_edits(path, [edit], compile=compile)


@mcp.tool()
async def lyx_insert_text(
    path: str,
    anchor: str,
    text: str,
    position: Literal["before", "after"],
    ctx: Context[AppContext],
    compile: bool = True,
) -> dict[str, object]:
    """Insert text before or after a unique plain-text anchor."""
    op: Literal["insert_before", "insert_after"] = "insert_before" if position == "before" else "insert_after"
    edit = EditSpec(op=op, anchor=anchor, text=text)
    return await ctx.request_context.lifespan_context.service.apply_edits(path, [edit], compile=compile)


@mcp.tool()
async def lyx_export(
    path: str,
    format: Literal["text", "latex", "pdf2"],
    ctx: Context[AppContext],
    output_path: str | None = None,
) -> dict[str, object]:
    """Export a LyX document as plain text, LaTeX, or PDF."""
    return await ctx.request_context.lifespan_context.service.export(path, format, output_path)


@mcp.tool()
async def lyx_validate_revision(
    path: str,
    ctx: Context[AppContext],
    master_path: str | None = None,
    checker_profile: str | None = None,
) -> dict[str, object]:
    """Check Track Changes and compile the document or its master to PDF."""
    return await ctx.request_context.lifespan_context.service.validate_revision(path, master_path, checker_profile)


@mcp.tool()
async def lyx_import_revision_range(
    path: str,
    source_path: str,
    start_heading: str,
    end_heading: str,
    expected_sha256: str,
    source_sha256: str,
    ctx: Context[AppContext],
    compile: bool = True,
) -> dict[str, object]:
    """Import tracked LyX section markup, including formulas and references, then validate it."""
    return await ctx.request_context.lifespan_context.service.import_revision_range(
        path, source_path, start_heading, end_heading, expected_sha256, source_sha256, compile
    )
