# LyX MCP Server

[Chinese README](README.zh-CN.md)

LyX MCP Server lets AI assistants edit LyX papers with tracked changes, including text and selected document structures, and export PDFs. It helps you review each change clearly and keep the paper ready for PDF export throughout revision.

## Prerequisites

- **Linux** with `/proc` and `prctl(PR_SET_PDEATHSIG)`. Process cleanup and the integration tests use Linux facilities. The installed LyX/Qt build must provide the offscreen platform plugin; this server currently supports only that backend.
- **LyX 2.4.x** installed locally. Set `[lyx].binary` to the absolute executable path. Another LyX version needs a dedicated `profile_seed_dir` and its own integration testing.
- **Python 3.11 or newer**, `venv`, and `pip` to install the MCP server and dependencies.
- **A working LaTeX toolchain** for `pdf2` export, including `pdflatex`, a bibliography tool such as `bibtex` for documents that use it, and the TeX packages required by the paper. Visible revisions require `xcolor.sty` and `ulem.sty`. Documents with SVG images may also need an SVG converter such as Inkscape.
- **Writable paths:** `[lyx].runtime_root` must be writable and permit FIFOs and subprocesses. Each paper and any persistent export destination must be inside a configured `[security].allowed_roots` directory. The server user needs read access to bibliography and image assets and write access to the paper directory. Also, be sure the path in the PWD environment variable exists and is currently set to a writable path.

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

Edit `config.toml`: set `binary` to the absolute LyX path, `runtime_root` to a writable directory (the default follows Python’s temporary-directory environment), and `allowed_roots` to the **absolute directories** containing the papers and persistent exports. The placeholder `/absolute/path/to/papers` must be replaced. A paper outside these roots is rejected. `config.toml` is not tracked by Git.

Configure your MCP client to start the server over **stdio**. Use the absolute path to this repository's `.venv/bin/lyx-mcp` as `command`, and set `LYX_MCP_CONFIG` to the absolute path of `config.toml` in its environment. The client launches the command when needed; a separate permanently running server is unnecessary. To run it manually, start the command below; it waits for MCP requests on stdin and can be stopped with Ctrl+C:

```bash
LYX_MCP_CONFIG="$PWD/config.toml" .venv/bin/lyx-mcp
```

The server writes only MCP protocol traffic to stdout. LyX stdout and stderr go to files in the isolated runtime directory.

## Agent interface

The default MCP surface has four tools:

| Tool | Purpose |
| --- | --- |
| `lyx_read` | Versioned state, outline, scoped text/source/nodes, search, native exports, cached builds and artifacts, optional PDF inspection |
| `lyx_apply_edits` | One version-checked mixed transaction with preflight, protection assertions, verification, one compilation, and optional final PDF delivery |
| `lyx_compare` | Two/three-way text hunks, unchanged context, conflicts, and selective reapplication |
| `lyx_job` | Status, bounded waiting, and cancellation with terminal transaction and recovery outcomes |

Read a Section or paragraph before editing. Node IDs are bound to its SHA256. The source-backed read avoids LyX IPC and hidden-note search ambiguity; whole-document native exports remain explicit requests. Responses are bounded and paginated.

```json
{
  "path": "/absolute/path/to/paper.lyx",
  "expected_sha256": "SHA256_FROM_READ",
  "operations": [
    {
      "id": "wording",
      "op": "replace_text",
      "node_id": "PARAGRAPH_ID_FROM_READ",
      "old_text": "The method achieve better results.",
      "new_text": "The method achieves better results."
    }
  ],
  "validation": {"compile": true, "preserve_unedited_insets": true},
  "output_path": "/absolute/path/to/final-tracked.pdf"
}
```

`operations` also supports citations, explicit paragraph splitting/joining, layout and passage moves, formula/reference updates, table cells/captions, reviewed Section imports, revision normalization, and selected comparison hunks. Cross-inset replacements retain typed inset tokens and list their IDs in `preserve_insets`. Every operation resolves against one baseline. Invalid operations are reported together before mutation. Structural operations report whether LyX can visibly track them.

The server applies source patches to a disposable candidate, loads and saves it once in isolated LyX, verifies accepted content and inset properties, and compiles before an atomic target replacement. Cancellation before commit leaves the target unchanged. Deadline/recovery results expose the failing stage, transaction status and whether retrying is safe. A Linux asynchronous pipe transport avoids blocking stdio worker reads.

A successful compile returns `build_id` and PDF/LaTeX/log/auxiliary handles. `lyx_read(validate=true)` and artifact export reuse the build while source and dependency hashes match. `lyx_read(build_id=...,artifact="manifest")` provides all retained file handles, diagnostics, and citation/reference mappings; textual artifact retrieval supports offsets and limits. `output_path` saves a new artifact in an allowed directory. Session artifacts otherwise expire at shutdown. A `pdf` optional dependency enables annotation extraction and page rendering through read modes; install it with `pip install -e '.[pdf]'`.

The Python service's older methods remain for compatibility; old MCP convenience tool names have been consolidated. Restart the MCP connection and update the bundled [editing skill](skill/lyx-paper-editing/SKILL.md) together. See [interface design and limits](docs/interface.md) for the contract. Scoped native LaTeX exports and automatic reapplication of structural/inset conflicts are not implemented; use scoped source/nodes and explicit operations for those cases. Multi-file master builds have a staging implementation but need project-specific integration checks.

## Process isolation and cleanup

Each server launch claims a slot in the private `0700` pool `<runtime_root>/lyx-mcp-<uid>/`. A persistent `<slot>.lock` file holds an exclusive, nonblocking `flock` for the lifetime of the session. Concurrent instances claim separate slots, and later sessions reuse the first available slot after clearing its previous contents. The lock file stays outside the slot's runtime directory and remains in place during cleanup. LyX inherits the lock descriptor, so the slot remains reserved until both the MCP server and its LyX child release it.

Each slot has a private runtime and userdir, a separate `\serverpipe "$$UserDir/run/lyxserver"`, and a `lyx --no-remote -userdir ...` process with `QT_QPA_PLATFORM=offscreen`. Tools share a single LyXServer client ID and an asynchronous lock. Internal LyX restarts reuse the same slot, reset the profile, pipes, logs, caches, and temporary compilation files, and retain exports and rollback snapshots until the session ends. Normal shutdown removes the runtime contents before releasing the lock. Abrupt shutdown leaves contents for cleanup when that slot is next claimed; slot count follows peak concurrent use rather than total launches.

For `/tmp` backed by tmpfs, `runtime_root` can also point to a writable directory on disk. Random-name `lyx-mcp-*` directories from earlier versions require separate cleanup after their owning instances have stopped.

On a normal shutdown, the server asks LyX to quit, sends SIGTERM to its **own** process group, and sends SIGKILL after a three-second grace period if that group remains. On Linux, the LyX launcher also sets a parent-death signal before executing LyX, so an abruptly killed MCP server does not leave its LyX child running. A previous version only attempted cleanup from the normal shutdown path and could leave an orphan if the MCP process died first. A CPU-heavy LyX process that ignores SIGTERM therefore triggers the SIGKILL fallback; its underlying busy-loop cause cannot be diagnosed without that process's log or stack trace. The server never signals unrelated desktop LyX processes.

Two instances can edit different files concurrently. If a desktop instance edits the **same file**, its unsaved, un-autosaved changes cannot be detected by this server; save and close that desktop buffer first. The server rejects a newer `#file.lyx#` autosave, a known disk SHA change, and external edits during a transaction.

The built-in profile is verified for local LyX 2.4.x. For another version, configure an independent `profile_seed_dir` and rerun real LyX integration tests. Systems that require Xvfb need an implemented and verified launcher for that backend.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m mypy src
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/python -m pytest -q
```

The default suite starts isolated local LyX processes and uses temporary `.lyx` copies to test revisions, PDF export, text edits, rollback, and process cleanup. The stdio MCP and LyX syntax tests need local IPC access:

```bash
LYX_MCP_RUN_STDIO_TEST=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_stdio.py tests/test_lyx_syntax.py
```
