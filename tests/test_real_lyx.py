import asyncio
import shutil
import stat
from pathlib import Path

import pytest

from lyx_mcp.config import CheckerProfile, Config
from lyx_mcp.document import fingerprint
from lyx_mcp.edit import EditSpec
from lyx_mcp.errors import ConflictError, LyXMCPError
from lyx_mcp.service import LyXService

FIXTURE = Path(__file__).parent / "fixtures" / "simple.lyx"


@pytest.mark.skipif(shutil.which("lyx") is None, reason="LyX is not installed")
def test_isolated_lyx_edit_compile_and_rollback(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = Config(
            allowed_roots=(tmp_path,),
            checker_profiles={"fail": CheckerProfile(("false",), 5)},
            export_timeout_sec=40,
        )
        service = LyXService(config)
        await service.start()
        try:
            layout = service.layout
            assert layout.userdir.is_relative_to(layout.root)
            assert stat.S_ISFIFO(Path(f"{layout.pipe_stem}.in").stat().st_mode)
            assert stat.S_ISFIFO(Path(f"{layout.pipe_stem}.out").stat().st_mode)
            assert "--no-remote" in service.runtime.process.args
            assert str(layout.userdir) in service.runtime.process.args
            lyx_pid = service.runtime.process.pid

            document = tmp_path / "edited.lyx"
            shutil.copy2(FIXTURE, document)
            baseline = document.read_bytes()
            result = await service.apply_edits(
                str(document),
                [EditSpec(op="replace", old_text="Gamma delta", new_text="Gamma epsilon")],
            )
            assert result["ok"] is True
            assert result["sha256_before"] != result["sha256_after"]
            assert b"\\change_deleted" in document.read_bytes()
            assert b"\\change_inserted" in document.read_bytes()
            assert b"\\output_changes true" in document.read_bytes()
            assert document.read_bytes() != baseline
            pdf = Path(result["pdf"])
            assert pdf.read_bytes().startswith(b"%PDF-")
            assert pdf.read_bytes().rstrip().endswith(b"%%EOF")
            assert "Gamma epsilon" in (await service.read(str(document)))["content"]
            second_pass = await service.apply_edits(
                str(document),
                [EditSpec(op="replace", old_text="Alpha beta", new_text="Alpha gamma")],
                compile=False,
            )
            assert second_pass["ok"] is True
            assert (await service.get_document_state(str(document)))["output_changes"] is True
            assert b"delta" in document.read_bytes()
            assert b"epsilon" in document.read_bytes()
            assert (await service.get_document_state(str(document)))["tracking_changes"] is True

            failed = tmp_path / "failed.lyx"
            shutil.copy2(FIXTURE, failed)
            original = failed.read_bytes()
            outcome = await service.apply_edits(
                str(failed),
                [EditSpec(op="replace", old_text="Gamma delta", new_text="Wrong")],
                checker_profile="fail",
            )
            assert outcome["ok"] is False
            assert outcome["error_code"] == "CHECKER_FAILED"
            assert outcome["rollback_verified"] is True
            assert failed.read_bytes() == original
            assert "Gamma delta" in (await service.read(str(failed)))["content"]

            broken = tmp_path / "broken.lyx"
            ert = (
                "\\begin_inset ERT\nstatus collapsed\n\n"
                "\\begin_layout Plain Layout\n\\backslash\nundefinedlyxmcpcommand\n"
                "\\end_layout\n\n\\end_inset\n"
            )
            broken.write_text(
                FIXTURE.read_text().replace(
                    "Alpha beta. Gamma delta. Alpha BETTER.",
                    f"Alpha beta. {ert} Gamma delta. Alpha BETTER.",
                )
            )
            with pytest.raises(LyXMCPError, match="Undefined control sequence"):
                await service.export(str(broken), "pdf2")
            broken_original = broken.read_bytes()
            rejected = await service.apply_edits(
                str(broken),
                [EditSpec(op="replace", old_text="Gamma delta", new_text="Gamma epsilon")],
            )
            assert rejected["ok"] is False
            assert rejected["error_code"] == "PDF_COMPILE_FAILED"
            assert rejected["rollback_verified"] is True
            assert broken.read_bytes() == broken_original

            missing_citation = tmp_path / "missing-citation.lyx"
            citation = (
                "\\begin_inset CommandInset citation\nLatexCommand cite\n"
                'key "NonexistentLyXMCPReference"\nliteral "false"\n\n\\end_inset'
            )
            missing_citation.write_text(
                FIXTURE.read_text().replace("Gamma delta", f"Gamma {citation} delta")
            )
            with pytest.raises(LyXMCPError) as undefined:
                await service.export(str(missing_citation), "pdf2")
            assert undefined.value.code == "UNDEFINED_CITATIONS"
        finally:
            await service.close()
        assert not layout.root.exists()
        assert not Path(f"/proc/{lyx_pid}").exists()

    asyncio.run(exercise())


@pytest.mark.skipif(shutil.which("lyx") is None, reason="LyX is not installed")
def test_real_lyx_text_primitives_and_second_isolated_runtime(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = Config(allowed_roots=(tmp_path,), export_timeout_sec=30)
        first = LyXService(config)
        second = LyXService(config)
        await first.start()
        await second.start()
        try:
            assert first.layout.userdir != second.layout.userdir
            assert first.layout.pipe_stem != second.layout.pipe_stem
            cases = [
                ("before", [EditSpec(op="insert_before", anchor="Gamma delta.", text="Before ")], "Before Gamma"),
                ("after", [EditSpec(op="insert_after", anchor="Alpha beta.", text=" Added.")], "beta. Added."),
                ("delete", [EditSpec(op="delete", old_text="Gamma delta.")], "Alpha beta.  Alpha"),
                (
                    "paragraph",
                    [EditSpec(op="insert_after", anchor="Alpha BETTER.", text="\nSecond paragraph.")],
                    "BETTER.\n\nSecond paragraph.",
                ),
            ]
            for name, edits, expected in cases:
                document = tmp_path / f"{name}.lyx"
                shutil.copy2(FIXTURE, document)
                result = await first.apply_edits(str(document), edits, compile=False)
                assert result["ok"] is True, result
                assert expected in (await first.read(str(document)))["content"]

            formula = tmp_path / "formula.lyx"
            formula.write_text(
                FIXTURE.read_text().replace(
                    "Alpha beta. Gamma delta.",
                    "Alpha \\begin_inset Formula $x$\n\\end_inset\n beta. Gamma delta.",
                )
            )
            formula_before = formula.read_bytes()
            with pytest.raises(LyXMCPError, match="contiguous plain text"):
                await first.apply_edits(
                    str(formula),
                    [EditSpec(op="replace", old_text="Alpha x beta", new_text="New phrase")],
                    compile=False,
                )
            assert formula.read_bytes() == formula_before
            assert "Gamma delta" in (await first.read(str(formula)))["content"]

            imported = tmp_path / "imported.lyx"
            donor = tmp_path / "donor.lyx"
            body = (
                "\\begin_layout Section\nStart\n\\end_layout\n"
                "\\begin_layout Standard\nAlpha \\begin_inset Formula $x$\n\\end_inset\n beta.\n\\end_layout\n"
                "\\begin_layout Section\nEnd\n\\end_layout\n"
                "\\begin_layout Standard\nOutside target.\n\\end_layout\n"
            )
            template = FIXTURE.read_text()
            original_body = "\\begin_layout Standard\nAlpha beta. Gamma delta. Alpha BETTER.\n\\end_layout\n"
            imported.write_text(template.replace(original_body, body))
            donor_body = body.replace(
                " beta.\n\\end_layout",
                " \\change_deleted 0 1\nbeta\n\\change_inserted 0 1\ngamma"
                "\n\\change_unchanged\n.\n\\end_layout",
            )
            donor.write_text(
                template.replace("\\end_header", '\\author 0 "Test" ""\n\\end_header').replace(
                    original_body, donor_body
                )
            )
            result = await first.import_revision_range(
                str(imported),
                str(donor),
                "Start",
                "End",
                fingerprint(imported),
                fingerprint(donor),
            )
            assert result["ok"] is True, result
            assert b"\\output_changes true" in imported.read_bytes()
            assert Path(result["pdf"]).read_bytes().startswith(b"%PDF-")
            assert b"\\begin_inset Formula $x$" in imported.read_bytes()
            assert b"\\change_inserted" in imported.read_bytes()
            assert "Alpha x gamma" in (await first.read(str(imported)))["content"]
            assert "Outside target." in (await first.read(str(imported)))["content"]
            other = tmp_path / "second.lyx"
            shutil.copy2(FIXTURE, other)
            assert "Gamma delta" in (await second.read(str(other)))["content"]

            external = tmp_path / "external.lyx"
            shutil.copy2(FIXTURE, external)
            await first.read(str(external))
            external.write_text(external.read_text().replace("Gamma delta", "External revision"))
            with pytest.raises(ConflictError, match="changed on disk"):
                await first.apply_edits(
                    str(external),
                    [EditSpec(op="replace", old_text="Gamma delta", new_text="Would overwrite")],
                    compile=False,
                )
            assert "External revision" in (await first.read(str(external)))["content"]
        finally:
            await first.close()
            await second.close()

    asyncio.run(exercise())


@pytest.mark.skipif(shutil.which("lyx") is None, reason="LyX is not installed")
def test_output_changes_unsafe_resizebox_is_repaired(tmp_path: Path) -> None:
    async def exercise() -> None:
        document = tmp_path / "unsafe.lyx"
        old = (
            "\\begin_inset ERT\nstatus open\n\n\\begin_layout Plain Layout\n"
            "\\backslash\nresizebox{0.5\\backslash\ntextwidth}{!}{\n\\end_layout\n\\end_inset"
        )
        new = (
            "\\begin_inset ERT\nstatus collapsed\n\n\\begin_layout Plain Layout\n"
            "\\backslash\nresizebox{\\backslash\ntextwidth}{!}{\n\\end_layout\n\\end_inset"
        )
        close = "\\begin_inset ERT\nstatus open\n\n\\begin_layout Plain Layout\n}\n\\end_layout\n\\end_inset"
        body = (
            "\\begin_layout Standard\n\\change_deleted 0 100\n"
            + old
            + "\n\\change_inserted 0 100\n"
            + new
            + "\n\\change_unchanged\nVisible text.\n"
            + close
            + "\n\\end_layout"
        )
        source = FIXTURE.read_text().replace(
            "\\begin_layout Standard\nAlpha beta. Gamma delta. Alpha BETTER.\n\\end_layout", body
        )
        source = source.replace("\\tracking_changes false", "\\tracking_changes true")
        source = source.replace(
            "\\tracking_changes true",
            "\\begin_preamble\n\\usepackage{graphicx}\n\\end_preamble\n\\tracking_changes true",
        )
        source = source.replace("\\end_header", '\\author 0 "Test" ""\n\\end_header')
        document.write_text(source)
        service = LyXService(Config(allowed_roots=(tmp_path,), export_timeout_sec=45))
        await service.start()
        try:
            baseline = (await service.read(str(document)))["content"]
            broken = tmp_path / "broken.lyx"
            broken.write_text(source.replace("\\output_changes false", "\\output_changes true"))
            with pytest.raises(LyXMCPError, match="File ended while scanning"):
                await service.export(str(broken), "pdf2")
            result = await service.normalize_revisions(str(document), fingerprint(document), 0, 100)
            assert result["ok"] is True, result
            assert result["accepted_structural_ert_replacements"] == 1
            assert result["output_changes"] is True
            assert (await service.read(str(document)))["content"] == baseline
            assert Path(result["pdf"]).read_bytes().startswith(b"%PDF-")
        finally:
            await service.close()

    asyncio.run(exercise())
