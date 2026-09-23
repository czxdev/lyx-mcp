# LyX MCP Server

[Chinese README](README.zh-CN.md)

LyX MCP Server lets AI assistants edit LyX papers with tracked changes, including text and selected document structures, and export PDFs. It helps you review each change clearly and keep the paper ready for PDF export throughout revision.

## Prerequisites

- **Linux** with `/proc` and `prctl(PR_SET_PDEATHSIG)`. Process cleanup and the integration tests use Linux facilities. The installed LyX/Qt build must provide the offscreen platform plugin; this server currently supports only that backend.
- **LyX 2.4.x** installed locally. Set `[lyx].binary` to the absolute executable path. Another LyX version needs a dedicated `profile_seed_dir` and its own integration testing.
- **Python 3.11 or newer**, `venv`, and `pip` to install the MCP server and dependencies.
- **A working LaTeX toolchain** for `pdf2` export, including `pdflatex`, a bibliography tool such as `bibtex` for documents that use it, and the TeX packages required by the paper. Visible revisions require `xcolor.sty` and `ulem.sty`. Documents with SVG images may also need an SVG converter such as Inkscape.
- **Writable paths:** `[lyx].runtime_root` must be writable and permit FIFOs and subprocesses. Each paper and any persistent export destination must be inside a configured `[security].allowed_roots` directory. The server user needs read access to bibliography and image assets and write access to the paper directory.

Check the base installation before configuring an MCP client:

```bash
python3 --version
lyx -version
command -v pdflatex
command -v bibtex
kpsewhich xcolor.sty
kpsewhich ulem.sty
```

A command that prints no path indicates a missing dependency needed for documents that use that feature. For an SVG-based paper, also check `command -v inkscape` or its configured converter.

## Install and connect

From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
```

For Codex, install the bundled [LyX paper editing skill](skill/lyx-paper-editing/SKILL.md) so it follows the paper editing workflow:

```bash
mkdir -p ~/.codex/skills
cp -a skill/lyx-paper-editing ~/.codex/skills/
```

If you use a custom Codex skills directory, copy the skill there instead. Other MCP clients can use the server without the skill.

Edit `config.toml`: set `binary` to the absolute LyX path, `runtime_root` to a writable directory (the default is `/tmp`), and `allowed_roots` to the **absolute directories** containing the papers and persistent exports. The placeholder `/absolute/path/to/papers` must be replaced. A paper outside these roots is rejected. `config.toml` is not tracked by Git.

Configure your MCP client to start the server over **stdio**. Use the absolute path to this repository's `.venv/bin/lyx-mcp` as `command`, and set `LYX_MCP_CONFIG` to the absolute path of `config.toml` in its environment. The client launches the command when needed; a separate permanently running server is unnecessary. To run it manually, start the command below; it waits for MCP requests on stdin and can be stopped with Ctrl+C:

```bash
LYX_MCP_CONFIG="$PWD/config.toml" .venv/bin/lyx-mcp
```

The server writes only MCP protocol traffic to stdout. LyX stdout and stderr go to files in the isolated runtime directory.

## Tools

| Tool | Purpose |
| --- | --- |
| `lyx_get_document_state` | Read path, SHA256, Track Changes settings, and autosave state |
| `lyx_read` | Export accepted plain text or LaTeX through LyX |
| `lyx_apply_edits` | Batch `replace`, `delete`, `insert_before`, and `insert_after` operations; compile by default |
| `lyx_replace_text`, `lyx_delete_text`, `lyx_insert_text` | Single-edit convenience tools |
| `lyx_export` | Export text, latex, or pdf2; optionally copy to a new file in an allowed directory |
| `lyx_validate_revision` | Check Track Changes and compile the current document or its master |
| `lyx_import_revision_range` | Import a tracked, continuous Section range from another `.lyx` file and compile |
| `lyx_normalize_revisions` | Merge fragmented revisions from a given author and timestamp, handle unsafe `resizebox` ERT replacements, and compile |

Example batch edit:

```json
{
  "path": "/absolute/path/to/paper.lyx",
  "edits": [
    {
      "op": "replace",
      "old_text": "The method achieve better results.",
      "new_text": "The method achieves better results."
    },
    {
      "op": "insert_after",
      "anchor": "This is the final paragraph.",
      "text": "\nA new paragraph."
    }
  ],
  "compile": true
}
```

Each target normally must be an exact, unique match in LyX's accepted plain-text export and map to continuous ordinary text in the `.lyx` source. Use `context_before` and `context_after`, then `occurrence`, to disambiguate repeated text. Replacement narrows identical prefixes and suffixes while preserving sensible word or numeric-range boundaries. A sentence rewrite remains one contiguous revision. Targets crossing formulas or other insets are rejected before the LyX search; use `lyx_import_revision_range` for reviewed structural revisions. A `\n` in inserted text separates paragraphs and is supported only at a paragraph boundary.

Import requires source and target SHA256 values and identical, unique start and end Section headings. It copies the LyX markup from the start Section up to the end Section, adds the revision author, then reopens, saves, and compiles the result in isolated LyX. The source range must already contain Track Changes markers and the target range must have none. The entire target range is replaced, without a three-way merge, so compare the full range first.

Successful edits and imports set `\tracking_changes true` and `\output_changes true`. Compilation checks both fatal TeX errors and undefined citations in the final log. `lyx_normalize_revisions` joins adjacent edits by the same author and timestamp into readable phrases or sentences while preserving the accepted and rejected text. It accepts unsafe `resizebox` ERT opening replacements as structural formatting changes and reports their count. The [LyX paper editing skill](skill/lyx-paper-editing/SKILL.md) gives revision-span guidance and lists tested LyX syntax. In ordinary LyX text and captions, use `10-50` for a numeric range; `10--50` is raw TeX input syntax and should not be copied into LyX prose.

`checker_profile` can select only a fixed command from the configuration. `master_path` can compile a master after a child edit; real multi-file integration remains unverified. Default `lyx_export` artifacts reside in the temporary runtime and are deleted when the server exits. To keep one, pass a new `output_path` within `allowed_roots`.

## Process isolation and cleanup

Each server launch creates a private `0700` runtime and userdir, configures a separate `\serverpipe "$$UserDir/run/lyxserver"`, and starts `lyx --no-remote -userdir ...` with `QT_QPA_PLATFORM=offscreen`. Tools share a single LyXServer client ID and an asynchronous lock. Desktop LyX instances are not reused or signaled.

On a normal shutdown, the server asks LyX to quit, sends SIGTERM to its **own** process group, and sends SIGKILL after a three-second grace period if that group remains. On Linux, the LyX launcher also sets a parent-death signal before executing LyX, so an abruptly killed MCP server does not leave its LyX child running. A previous version only attempted cleanup from the normal shutdown path and could leave an orphan if the MCP process died first. A CPU-heavy LyX process that ignores SIGTERM therefore triggers the SIGKILL fallback; its underlying busy-loop cause cannot be diagnosed without that process's log or stack trace. The server never signals unrelated desktop LyX processes.

Two instances can edit different files concurrently. If a desktop instance edits the **same file**, its unsaved, un-autosaved changes cannot be detected by this server; save and close that desktop buffer first. The server rejects a newer `#file.lyx#` autosave, a known disk SHA change, and external edits during a transaction.

The built-in profile is verified for local LyX 2.4.x. For another version, configure an independent `profile_seed_dir` and rerun real LyX integration tests. Systems that require Xvfb need an implemented and verified launcher for that backend.

## Tests

```bash
PYTHONPATH=src .venv/bin/mypy src
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/pytest -q
```

The default suite starts isolated local LyX processes and uses temporary `.lyx` copies to test revisions, PDF export, text edits, rollback, and process cleanup. The stdio MCP and LyX syntax tests need local IPC access:

```bash
LYX_MCP_RUN_STDIO_TEST=1 PYTHONPATH=src .venv/bin/pytest -q tests/test_stdio.py tests/test_lyx_syntax.py
```
