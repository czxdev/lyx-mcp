import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from lyx_mcp.document import fingerprint
from lyx_mcp.index import DocumentIndex

FIXTURE = Path(__file__).parent / "fixtures" / "simple.lyx"


def server(tmp_path: Path) -> StdioServerParameters:
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lyx]\nruntime_root = {json.dumps(str(tmp_path.parent))}\n"
        f"[security]\nallowed_roots = [{json.dumps(str(tmp_path))}]\n"
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "lyx_mcp"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src"), "LYX_MCP_CONFIG": str(config)},
        cwd=Path(__file__).parent.parent,
    )


@pytest.mark.skipif(os.environ.get("LYX_MCP_RUN_STDIO_TEST") != "1", reason="run this test with full local IPC access")
def test_stdio_mcp_edit_and_read(tmp_path: Path) -> None:
    async def exercise() -> None:
        document = tmp_path / "document.lyx"
        shutil.copy2(FIXTURE, document)
        async with Client(server(tmp_path)) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {"lyx_read", "lyx_apply_edits", "lyx_compare", "lyx_job"}
            edited = await client.call_tool(
                "lyx_apply_edits",
                {
                    "path": str(document),
                    "expected_sha256": fingerprint(document),
                    "operations": [
                        {"id": "grammar", "op": "replace_text", "old_text": "Gamma delta", "new_text": "Gamma epsilon"}
                    ],
                    "wait_sec": 20,
                },
            )
            assert edited.structured_content["ok"] is True
            read = await client.call_tool("lyx_read", {"path": str(document)})
            assert "Gamma epsilon" in read.structured_content["passages"][0]["text"]

    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get("LYX_MCP_RUN_STDIO_TEST") != "1", reason="run this test with full local IPC access")
def test_replay_regressions_in_one_mcp_transaction(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "regressions.lyx"
        source = FIXTURE.read_text().replace("\\end_header", '\\author 0 "Author" ""\n\\end_header')
        body = (
            "\\begin_layout Standard\nFinished\\SpecialChar endofsentence\n"
            "\\begin_inset Note Note\nstatus collapsed\n\\begin_layout Plain Layout\nKeep hidden.\n"
            "\\end_layout\n\\end_inset\n After.\n\\end_layout\n"
            "\\begin_layout Standard\n\\change_inserted 0 1\nFirst added. Second added.\n"
            "\\change_unchanged\n Original tail.\n\\end_layout\n"
            "\\begin_layout Standard\n\\begin_inset Float table\nplacement document\nalignment document\n"
            "wide false\nsideways false\nstatus open\n\\begin_layout Plain Layout\n"
            "\\begin_inset Caption Standard\n\\begin_layout Plain Layout\nOriginal caption\n"
            '\\begin_inset CommandInset label\nLatexCommand label\nname "tab:caption"\n\\end_inset\n'
            "\\end_layout\n\\end_inset\n\\end_layout\n\\end_inset\n\\end_layout\n"
        )
        source = source[: source.index("\\begin_body")] + "\\begin_body\n" + body + "\\end_body\n\\end_document\n"
        path.write_text(source)
        async with Client(server(tmp_path)) as client:
            read = (await client.call_tool("lyx_read", {"path": str(path), "max_chars": 16000})).structured_content
            assert read["passages"][0]["text"].startswith("Finished.")
            index = DocumentIndex(source)
            caption = next(p for p in index.paragraphs if index.text(p).startswith("Original caption"))
            result = await client.call_tool(
                "lyx_apply_edits",
                {
                    "path": str(path),
                    "expected_sha256": read["state"]["sha256"],
                    "operations": [
                        {"id": "period", "op": "replace_text", "node_id": index.top[0].id,
                         "old_text": ".", "new_text": ", as specified.", "occurrence": 1},
                        {"id": "space", "op": "replace_text", "node_id": index.top[0].id,
                         "old_text": " ", "new_text": "For ", "occurrence": 1},
                        {"id": "split", "op": "split_paragraph", "node_id": index.top[1].id,
                         "after_text": "First added."},
                        {"id": "caption", "op": "replace_text", "node_id": caption.id,
                         "old_text": "Original caption", "new_text": "Original caption. Added explanation."},
                    ],
                    "wait_sec": 30,
                    "timeout_sec": 45,
                    "output_path": str(tmp_path / "final.pdf"),
                },
            )
            data = result.structured_content
            assert data["ok"], data
            assert data["validation"]["revisions_verified"]
            revised = DocumentIndex(path.read_text())
            assert revised.text(revised.top[0]).endswith("For After.")
            assert revised.text(revised.top[2]) == "Second added. Original tail."
            assert "Second added" not in str(revised.rejected_signature())
            assert "Added explanation" not in str(revised.rejected_signature())
            latex = (
                await client.call_tool("lyx_read", {"path": str(path), "format": "latex", "content_limit": 12000})
            ).structured_content
            assert latex["build"]["reused"]
            assert re.search(r"\\lyxadded[^\n]*Second added", latex["content"])
            assert (tmp_path / "final.pdf").read_bytes().startswith(b"%PDF-")

    asyncio.run(exercise())
