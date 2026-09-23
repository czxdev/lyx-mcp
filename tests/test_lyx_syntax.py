import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from lyx_mcp.document import fingerprint

FIXTURE = Path(__file__).parent / "fixtures" / "simple.lyx"
RUN = os.environ.get("LYX_MCP_RUN_STDIO_TEST") == "1" and shutil.which("lyx") is not None
pytestmark = pytest.mark.skipif(not RUN, reason="LyX MCP stdio integration requires local IPC")


def _document(body: str, author: bool = False) -> str:
    source = FIXTURE.read_text()
    source = source.replace(
        "\\begin_layout Standard\nAlpha beta. Gamma delta. Alpha BETTER.\n\\end_layout",
        body,
    )
    if author:
        source = source.replace("\\end_header", '\\author 0 "Test" ""\n\\end_header')
    return source


def _server(tmp_path: Path) -> StdioServerParameters:
    config = tmp_path / "config.toml"
    config.write_text(f"[lyx]\nexport_timeout_sec = 60\n[security]\nallowed_roots = [{json.dumps(str(tmp_path))}]\n")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "lyx_mcp"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
            "LYX_MCP_CONFIG": str(config),
        },
        cwd=Path(__file__).parent.parent,
    )


@pytest.mark.parametrize("layout", ["Standard", "Section", "Subsection", "Itemize", "Enumerate"])
def test_plain_text_layout_syntax_via_mcp(tmp_path: Path, layout: str) -> None:
    async def exercise() -> None:
        document = tmp_path / f"{layout}.lyx"
        document.write_text(_document(f"\\begin_layout {layout}\nAlpha beta is precise.\n\\end_layout"))
        async with Client(_server(tmp_path)) as client:
            result = await client.call_tool(
                "lyx_apply_edits",
                {
                    "path": str(document),
                    "edits": [
                        {
                            "op": "replace",
                            "old_text": "Alpha beta is precise.",
                            "new_text": "Alpha gamma is precise.",
                        }
                    ],
                },
            )
            assert result.structured_content["ok"] is True, result.structured_content
            assert result.structured_content["output_changes"] is True
            assert b"\\output_changes true" in document.read_bytes()
            assert Path(result.structured_content["pdf"]).read_bytes().startswith(b"%PDF-")
            latex = await client.call_tool("lyx_read", {"path": str(document), "format": "latex"})
            assert "gamma" in latex.structured_content["content"]

    asyncio.run(exercise())


def _citation(key: str) -> str:
    return f'\\begin_inset CommandInset citation\nLatexCommand cite\nkey "{key}"\nliteral "false"\n\n\\end_inset'


def _reference(name: str) -> str:
    return (
        "\\begin_inset CommandInset ref\nLatexCommand ref\n"
        f'reference "{name}"\nplural "false"\ncaps "false"\nnoprefix "false"\nnolink "false"\n\n\\end_inset'
    )


def _tabular(text: str) -> str:
    return (
        '\\begin_inset Tabular\n<lyxtabular version="3" rows="1" columns="1">\n'
        '<features tabularvalignment="middle">\n<column alignment="center" valignment="top">\n'
        '<row>\n<cell alignment="center" valignment="top" usebox="none">\n'
        "\\begin_inset Text\n\n\\begin_layout Plain Layout\n"
        f"{text}\n\\end_layout\n\n\\end_inset\n</cell>\n</row>\n</lyxtabular>\n\n\\end_inset"
    )


def _note(text: str) -> str:
    return (
        "\\begin_inset Note Note\nstatus collapsed\n\n\\begin_layout Plain Layout\n"
        f"{text}\n\\end_layout\n\n\\end_inset"
    )


def _ert(text: str) -> str:
    return (
        "\\begin_inset ERT\nstatus collapsed\n\n\\begin_layout Plain Layout\n"
        f"\\backslash\n{text}\n\\end_layout\n\n\\end_inset"
    )


@pytest.mark.parametrize(
    ("syntax", "old", "new"),
    [
        ("formula", "\\begin_inset Formula $x$\n\\end_inset", "\\begin_inset Formula $y$\n\\end_inset"),
        ("citation", _citation("A"), _citation("B")),
        ("reference", _reference("sec:first"), _reference("sec:second")),
        ("note", _note("Old note."), _note("New note.")),
        ("tabular", _tabular("Old cell"), _tabular("New cell")),
        ("ert", _ert("vspace{0pt}"), _ert("vspace{1pt}")),
    ],
)
def test_tracked_inset_syntax_via_mcp_import(tmp_path: Path, syntax: str, old: str, new: str) -> None:
    async def exercise() -> None:
        target = tmp_path / f"{syntax}-target.lyx"
        donor = tmp_path / f"{syntax}-donor.lyx"
        suffix = (
            "\\begin_layout Section\nEnd\n\\end_layout\n"
            "\\begin_layout Standard\n"
            "\\begin_inset CommandInset label\nLatexCommand label\nname \"sec:first\"\n\n\\end_inset\n"
            "\\begin_inset CommandInset label\nLatexCommand label\nname \"sec:second\"\n\n\\end_inset\n"
            "\\end_layout\n"
        )
        if syntax == "citation":
            (tmp_path / "refs.bib").write_text(
                "@article{A, title={A}, author={A}, year={2020}}\n"
                "@article{B, title={B}, author={B}, year={2021}}\n"
            )
            suffix += (
                "\\begin_layout Standard\n\\begin_inset CommandInset bibtex\nLatexCommand bibtex\n"
                'btprint "btPrintCited"\nbibfiles "refs"\noptions "plain"\nencoding "default"\n\n'
                "\\end_inset\n\\end_layout\n"
            )
        start = "\\begin_layout Section\nStart\n\\end_layout\n"
        target_body = start + "\\begin_layout Standard\nBefore " + old + " after.\n\\end_layout\n" + suffix
        donor_body = (
            start
            + "\\begin_layout Standard\nBefore \n\\change_deleted 0 100\n"
            + old
            + "\n\\change_inserted 0 100\n"
            + new
            + "\n\\change_unchanged\n after.\n\\end_layout\n"
            + suffix
        )
        target.write_text(_document(target_body))
        donor.write_text(_document(donor_body, author=True))
        async with Client(_server(tmp_path)) as client:
            result = await client.call_tool(
                "lyx_import_revision_range",
                {
                    "path": str(target),
                    "source_path": str(donor),
                    "start_heading": "Start",
                    "end_heading": "End",
                    "expected_sha256": fingerprint(target),
                    "source_sha256": fingerprint(donor),
                },
            )
            assert result.structured_content["ok"] is True, result.structured_content
            assert result.structured_content["output_changes"] is True
            assert Path(result.structured_content["pdf"]).read_bytes().startswith(b"%PDF-")
            assert (b"vspace{1pt}" if syntax == "ert" else new.encode()) in target.read_bytes()

    asyncio.run(exercise())


def test_fragmented_text_revision_normalization_via_mcp(tmp_path: Path) -> None:
    async def exercise() -> None:
        document = tmp_path / "fragmented.lyx"
        body = (
            "\\begin_layout Standard\n"
            "\\change_deleted 0 100\nWe\n\\change_inserted 0 100\nThey\n\\change_unchanged\n \n"
            "\\change_deleted 0 100\nuse\n\\change_inserted 0 100\ntest\n\\change_unchanged\n \n"
            "\\change_deleted 0 100\nwords\n\\change_inserted 0 100\nphrases\n\\change_unchanged\n"
            "\\end_layout"
        )
        document.write_text(_document(body, author=True).replace("\\tracking_changes false", "\\tracking_changes true"))
        async with Client(_server(tmp_path)) as client:
            before = await client.call_tool("lyx_read", {"path": str(document)})
            result = await client.call_tool(
                "lyx_normalize_revisions",
                {
                    "path": str(document),
                    "expected_sha256": fingerprint(document),
                    "author_id": 0,
                    "timestamp": 100,
                },
            )
            assert result.structured_content["ok"] is True, result.structured_content
            assert result.structured_content["merged_groups"] == 1
            assert result.structured_content["output_changes"] is True
            assert Path(result.structured_content["pdf"]).read_bytes().startswith(b"%PDF-")
            after = await client.call_tool("lyx_read", {"path": str(document)})
            assert before.structured_content["content"] == after.structured_content["content"]

    asyncio.run(exercise())
