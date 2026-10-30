from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from .config import load_config
from .service import LyXService


@dataclass
class AppContext:
    service: LyXService


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    service = LyXService(load_config())
    try:
        yield AppContext(service)
    finally:
        await service.close()


mcp = MCPServer("lyx-mcp", lifespan=lifespan)


@mcp.tool()
async def lyx_read(
    path: str,
    ctx: Context[AppContext],
    selector: dict[str, Any] | None = None,
    search: str | None = None,
    representations: list[str] | None = None,
    max_chars: int = 12000,
    content_offset: int = 0,
    content_limit: int = 4000,
    format: Literal["text", "latex"] | None = None,
    build: bool = False,
    validate: bool = False,
    build_id: str | None = None,
    artifact: str | None = None,
    output_path: str | None = None,
    pdf: dict[str, Any] | None = None,
    timeout_sec: float = 180,
    wait_sec: float = 1,
) -> dict[str, Any]:
    """Read versioned state/outline and scoped text/source/nodes without LyX IPC.

    selector: node_ids, section (ID or exact title), from_node, to_before_node,
    offset and limit. Inset tokens in text can be retained by editing operations.
    Native format exports are explicit whole-document requests, bounded by
    content_offset/content_limit. build/validate retain one reusable compilation;
    build_id+artifact reads its outputs or saves one to a new output_path.
    pdf optionally extracts annotations or renders pages (requires the pdf extra).
    Slow requests return job_id; use lyx_job for status, bounded wait or cancel.
    """
    service = ctx.request_context.lifespan_context.service
    return await service.jobs.submit(
        "read",
        lambda: service.read_document(
            path,
            selector,
            search,
            representations,
            max_chars,
            format,
            build,
            build_id,
            artifact,
            output_path,
            content_offset,
            content_limit,
            validate,
            pdf,
        ),
        timeout_sec,
        wait_sec,
    )


@mcp.tool()
async def lyx_apply_edits(
    path: str,
    expected_sha256: str,
    operations: list[dict[str, Any]],
    ctx: Context[AppContext],
    protect: list[dict[str, str]] | None = None,
    validation: dict[str, Any] | None = None,
    assertions: dict[str, Any] | None = None,
    output_path: str | None = None,
    master_path: str | None = None,
    checker_profile: str | None = None,
    timeout_sec: float = 180,
    wait_sec: float = 1,
) -> dict[str, Any]:
    """Preflight one version-bound mixed batch; verify/compile once, then commit atomically.

    Each operation needs id and op. Supported: replace_text/delete_text,
    insert_before/insert_after, replace_selection (preserve_insets + inset tokens),
    insert_citation, set_citation, set_reference/set_inset_properties, edit_formula,
    split_paragraph, join_paragraphs, set_layout, move_passage, edit_table_cell
    (zero-based row/column), set_caption, import_revision_range,
    normalize_revisions, and apply_hunk (compare_id/hunk_id).
    Nodes and selections always refer to the baseline read, even within a batch.
    protect uses from_node/to_before_node. assertions: unchanged_insets,
    only_regions, tracking_enabled. validation: compile (default true),
    visible_revisions, preserve_unedited_insets, no_unresolved.
    output_path optionally delivers the final PDF to a new allowed path.
    Structural operations explicitly report their visible tracking limitations.
    After cancellation, inspect transaction_status before considering a retry.
    """
    service = ctx.request_context.lifespan_context.service
    return await service.jobs.submit(
        "edit",
        lambda: service.transact(
            path,
            expected_sha256,
            operations,
            protect,
            validation,
            assertions,
            output_path,
            master_path,
            checker_profile,
        ),
        timeout_sec,
        wait_sec,
    )


@mcp.tool()
async def lyx_compare(
    path: str,
    revised_path: str,
    ctx: Context[AppContext],
    base_path: str | None = None,
    offset: int = 0,
    limit: int = 20,
    max_chars: int = 12000,
    timeout_sec: float = 60,
    wait_sec: float = 1,
) -> dict[str, Any]:
    """Compare base -> previous revision -> current path; return reviewable small hunks.

    Omitting base_path makes a two-way comparison. apply_hunk selectively reapplies
    safe text hunks against the returned current version. Current-author overlap,
    paragraph restructuring and changed inset contents are flagged for review.
    """
    service = ctx.request_context.lifespan_context.service
    return await service.jobs.submit(
        "compare",
        lambda: service.compare(path, revised_path, base_path, offset, limit, max_chars),
        timeout_sec,
        wait_sec,
    )


@mcp.tool()
async def lyx_job(
    job_id: str,
    ctx: Context[AppContext],
    action: Literal["status", "wait", "cancel"] = "status",
    wait_sec: float = 0,
) -> dict[str, Any]:
    """Inspect, wait at most 30 seconds, or cancel a job; terminal outcomes remain queryable."""
    return await ctx.request_context.lifespan_context.service.jobs.inspect(job_id, action, wait_sec)
