import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

FIXTURE = Path(__file__).parent / "fixtures" / "simple.lyx"


@pytest.mark.skipif(os.environ.get("LYX_MCP_RUN_STDIO_TEST") != "1", reason="run this test with full local IPC access")
def test_stdio_mcp_edit_and_read(tmp_path: Path) -> None:
    async def exercise() -> None:
        document = tmp_path / "document.lyx"
        shutil.copy2(FIXTURE, document)
        config = tmp_path / "config.toml"
        config.write_text(f"[security]\nallowed_roots = [{json.dumps(str(tmp_path))}]\n")
        environment = dict(
            os.environ,
            PYTHONPATH=str(Path(__file__).parent.parent / "src"),
            LYX_MCP_CONFIG=str(config),
        )
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lyx_mcp"],
            env=environment,
            cwd=Path(__file__).parent.parent,
        )
        async with Client(server) as client:
            listed = await client.list_tools()
            assert "lyx_apply_edits" in {tool.name for tool in listed.tools}
            assert "lyx_import_revision_range" in {tool.name for tool in listed.tools}
            edited = await client.call_tool(
                "lyx_apply_edits",
                {
                    "path": str(document),
                    "edits": [{"op": "replace", "old_text": "Gamma delta", "new_text": "Gamma epsilon"}],
                },
            )
            assert edited.structured_content["ok"] is True
            read = await client.call_tool("lyx_read", {"path": str(document)})
            assert "Gamma epsilon" in read.structured_content["content"]

    asyncio.run(exercise())
