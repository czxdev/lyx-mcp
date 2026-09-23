import os
from pathlib import Path

import pytest

from lyx_mcp.document import (
    authorize,
    fingerprint,
    header,
    import_tracked_range,
    reject_newer_autosave,
    restore,
    snapshot,
)
from lyx_mcp.edit import EditSpec, plan, validate_source_spans
from lyx_mcp.errors import ConflictError, LyXMCPError
from lyx_mcp.protocol import command, parse
from lyx_mcp.revision import normalize_revision_markup

FIXTURE = Path(__file__).parent / "fixtures" / "simple.lyx"


def test_protocol_framing_and_response() -> None:
    assert command("client", "self-insert", "é:a") == b"LYXCMD:client:self-insert:\xc3\xa9:a\n"
    assert parse(b"INFO:client:server-get-filename:/tmp/a:b.lyx\n").payload == "/tmp/a:b.lyx"
    assert parse(b"LYXSRV:client:hello\n").function == "hello"
    with pytest.raises(LyXMCPError, match="line break"):
        command("client", "self-insert", "a\nb")
    with pytest.raises(LyXMCPError, match="invalid LyXServer"):
        command("bad:client", "self-insert", "a")


def test_planner_disambiguation_and_overlap() -> None:
    baseline = "Alpha beta. Alpha BETA. alpha tail. Gamma.\n\n"
    with pytest.raises(LyXMCPError, match="2 matches"):
        plan(baseline, [EditSpec(op="replace", old_text="Alpha", new_text="New")])
    edits, expected = plan(
        baseline,
        [
            EditSpec(op="replace", old_text="Alpha", new_text="New", context_after=" beta."),
            EditSpec(op="replace", old_text="Gamma", new_text="Delta"),
        ],
    )
    assert [edit.index for edit in edits] == [1, 0]
    assert expected == "New beta. Alpha BETA. alpha tail. Delta.\n\n"
    with pytest.raises(LyXMCPError, match="overlap"):
        plan(
            baseline,
            [
                EditSpec(op="replace", old_text="Alpha beta", new_text="x"),
                EditSpec(op="delete", old_text="beta"),
            ],
        )


def test_multiline_requires_paragraph_boundary() -> None:
    with pytest.raises(LyXMCPError, match="paragraph boundary"):
        plan("Alpha beta. Gamma.\n\n", [EditSpec(op="insert_after", anchor="Alpha", text="A\nB")])
    _, expected = plan("Alpha beta.\n\n", [EditSpec(op="insert_after", anchor="Alpha beta.", text="\nMore")])
    assert expected == "Alpha beta.\n\nMore\n\n"


def test_replacement_granularity_matches_the_real_change() -> None:
    sentence = "The method achieve better results."
    edits, expected = plan(
        sentence,
        [EditSpec(op="replace", old_text=sentence, new_text="The method achieves better results.")],
    )
    assert edits[0].target == "achieve"
    assert edits[0].replacement == "achieves"
    assert expected == "The method achieves better results."
    rewritten, _ = plan(
        sentence,
        [EditSpec(op="replace", old_text=sentence, new_text="Our revised method performs well in the experiments.")],
    )
    assert rewritten[0].target == sentence[:-1]
    with pytest.raises(LyXMCPError, match="identical"):
        plan(sentence, [EditSpec(op="replace", old_text=sentence, new_text=sentence)])
    dash, _ = plan("Delays of 10--50 rounds.", [EditSpec(op="replace", old_text="10--50", new_text="10-50")])
    assert dash[0].target == "10--50"
    assert dash[0].replacement == "10-50"


def test_normalize_fragmented_text_and_unsafe_ert() -> None:
    source = (
        b"\\change_deleted 0 100\nWe\n\\change_inserted 0 100\nThey\n\\change_unchanged\n \n"
        b"\\change_deleted 0 100\nuse\n\\change_inserted 0 100\ntest\n\\change_unchanged\n \n"
        b"\\change_deleted 0 100\nwords\n\\change_inserted 0 100\nphrases\n\\change_unchanged\n"
        b"\\change_deleted 0 100\n\\begin_inset ERT\nstatus open\n"
        b"\\begin_layout Plain Layout\n\\backslash\nresizebox{0.5\\backslash\ntextwidth}{!}{\n"
        b"\\end_layout\n\\end_inset\n"
        b"\\change_inserted 0 100\n\\begin_inset ERT\nstatus collapsed\n"
        b"\\begin_layout Plain Layout\n\\backslash\nresizebox{\\backslash\ncolumnwidth}{!}{\n"
        b"\\end_layout\n\\end_inset\n\\change_unchanged\n"
    )
    normalized, groups, accepted = normalize_revision_markup(source, 0, 100)
    assert groups == 1
    assert accepted == 1
    assert normalized.count(b"\\change_deleted") == 1
    assert b"We\n \nuse\n \nwords" in normalized
    assert b"They\n \ntest\n \nphrases" in normalized
    assert b"resizebox{0.5" not in normalized
    assert b"resizebox{\\backslash\ncolumnwidth}" in normalized


def test_formula_spanning_text_is_rejected_before_edit() -> None:
    baseline = "Alpha x beta.\n\n"
    edits, _ = plan(baseline, [EditSpec(op="replace", old_text="Alpha x beta", new_text="New")])
    source = "Alpha \\begin_inset Formula $x$\n\\end_inset\n beta."
    with pytest.raises(LyXMCPError, match="contiguous plain text"):
        validate_source_spans(source, baseline, edits)


def test_import_tracked_range_keeps_formula_and_target_outside_sections() -> None:
    target = (
        b"\\begin_header\r\n\\end_header\r\n"
        b"\\begin_layout Section\r\nStart\r\n\\end_layout\r\n"
        b"\\begin_layout Standard\r\nAlpha beta\r\n\\end_layout\r\n"
        b"\\begin_layout Section\r\nEnd\r\n\\end_layout\r\n"
        b"Outside target\r\n"
    )
    source = (
        b"\\begin_header\n\\author 0 \"Test\" \"\"\n\\end_header\n"
        b"\\begin_layout Section\nStart\n\\end_layout\n"
        b"\\begin_layout Standard\nAlpha \\begin_inset Formula $x$\n\\end_inset\n"
        b"\\change_deleted 0 1\nbeta\n\\change_inserted 0 1\ngamma\n"
        b"\\change_unchanged\n\\end_layout\n"
        b"\\begin_layout Section\nEnd\n\\end_layout\n"
        b"Outside source\n"
    )
    merged, changes = import_tracked_range(target, source, "Start", "End")
    assert changes == 2
    assert b"\\begin_inset Formula $x$\r\n" in merged
    assert b"\\author 0 \"Test\" \"\"\r\n" in merged
    assert merged.endswith(b"Outside target\r\n")
    assert b"\n" not in merged.replace(b"\r\n", b"")


def test_document_guard_and_exact_restore(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    document = allowed / "simple.lyx"
    document.write_bytes(FIXTURE.read_bytes())
    assert authorize(document, (allowed,)) == document
    assert header(document).tracking_changes is False
    original = fingerprint(document)
    saved = snapshot(document, tmp_path)
    document.write_text("changed")
    restore(document, saved)
    assert fingerprint(document) == original
    external = tmp_path / "external.lyx"
    external.write_bytes(FIXTURE.read_bytes())
    (allowed / "escape.lyx").symlink_to(external)
    with pytest.raises(LyXMCPError, match="outside allowed_roots"):
        authorize(allowed / "escape.lyx", (allowed,))
    autosave = allowed / "#simple.lyx#"
    autosave.write_bytes(b"unsaved")
    new_time = document.stat().st_mtime_ns + 1_000_000_000
    os.utime(autosave, ns=(new_time, new_time))
    with pytest.raises(ConflictError, match="newer LyX autosave"):
        reject_newer_autosave(document)
