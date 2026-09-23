from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

from .client import LyXClient
from .config import Config
from .document import (
    assert_unchanged,
    authorize,
    fingerprint,
    header,
    import_tracked_range,
    reject_newer_autosave,
    restore,
    section_span,
    snapshot,
    write_bytes_atomically,
)
from .edit import EditSpec, apply, plan, validate_source_spans
from .errors import ConflictError, LyXMCPError
from .revision import normalize_revision_markup
from .runtime import Layout, Runtime

ExportFormat = Literal["text", "latex", "pdf2"]
SUFFIX = {"text": ".txt", "latex": ".tex", "pdf2": ".pdf"}


def _raise_fresh_pdf_errors(temp: Path, started: int) -> None:
    for log in temp.rglob("*.log"):
        if log.stat().st_mtime_ns < started:
            continue
        content = log.read_text(errors="replace")
        errors = [line for line in content.splitlines() if line.startswith("!")]
        if errors or "Fatal error occurred" in content:
            raise LyXMCPError("PDF_COMPILE_FAILED", "; ".join(errors[:5]) or "fatal TeX error")


def _pdf_has_clean_log(output: Path, temp: Path, started: int) -> bool:
    output_hash = fingerprint(output)
    for generated in temp.rglob("*.pdf"):
        details = generated.stat()
        if details.st_mtime_ns < started or details.st_size != output.stat().st_size:
            continue
        if fingerprint(generated) != output_hash:
            continue
        log = generated.with_suffix(".log")
        if not log.exists() or log.stat().st_mtime_ns < started:
            continue
        content = log.read_text(errors="replace")
        citations = re.findall(r"LaTeX Warning: Citation `([^']+)'[^\n]*undefined", content)
        if citations:
            raise LyXMCPError("UNDEFINED_CITATIONS", ", ".join(sorted(set(citations))))
        if "Output written on" in content:
            return True
    return False


class LyXService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runtime = Runtime(config)
        self.lock = asyncio.Lock()
        self._known: dict[Path, str] = {}
        self._opened: set[Path] = set()
        self._conflicted: set[Path] = set()

    async def start(self) -> None:
        await self.runtime.start()

    async def close(self) -> None:
        await self.runtime.close()

    @property
    def client(self) -> LyXClient:
        client = self.runtime.client
        if client is None:
            raise LyXMCPError("SESSION_BROKEN", "LyX session has not started")
        return client

    @property
    def layout(self) -> Layout:
        layout = self.runtime.layout
        if layout is None:
            raise LyXMCPError("SESSION_BROKEN", "LyX runtime has not started")
        return layout

    async def _ensure_buffer(self, path: Path) -> None:
        current = await self.client.call("server-get-filename")
        if current and Path(current).resolve() == path:
            return
        if path in self._opened:
            await self.client.call("buffer-switch", str(path))
        else:
            await self.client.call("file-open", str(path))
            self._opened.add(path)
        current = await self.client.call("server-get-filename")
        if not current or Path(current).resolve() != path:
            raise LyXMCPError("WRONG_BUFFER", f"LyX opened {current!r} instead of {path}")

    async def _refresh_if_changed(self, path: Path) -> None:
        known = self._known.get(path)
        if known is None or fingerprint(path) == known:
            return
        await self._ensure_buffer(path)
        await self.client.call("buffer-reload", "dump")
        self._known[path] = fingerprint(path)

    async def _export(self, path: Path, format: ExportFormat) -> Path:
        await self._ensure_buffer(path)
        output = self.layout.exports / f"{uuid.uuid4().hex}{SUFFIX[format]}"
        if format == "pdf2":
            await self.client.call("buffer-reset-export")
        started = time.time_ns()
        await self.client.call("buffer-export", f"{format} {output}", timeout=self.config.export_timeout_sec)
        deadline = time.monotonic() + self.config.export_timeout_sec
        last_size = -1
        steady = 0
        while time.monotonic() < deadline:
            if self.runtime.process is None or self.runtime.process.poll() is not None:
                raise LyXMCPError("LYX_EXITED", "LyX exited during export")
            if format == "pdf2":
                try:
                    _raise_fresh_pdf_errors(self.layout.temp, started)
                except LyXMCPError:
                    await self.runtime.restart()
                    self._opened.clear()
                    raise
            if output.exists():
                details = output.stat()
                if details.st_size > 0 and details.st_mtime_ns >= started:
                    steady = steady + 1 if details.st_size == last_size else 0
                    if format == "pdf2":
                        with output.open("rb") as pdf:
                            starts_pdf = pdf.read(5) == b"%PDF-"
                            pdf.seek(max(0, details.st_size - 128))
                            ends_pdf = pdf.read().rstrip().endswith(b"%%EOF")
                        complete_pdf = starts_pdf and ends_pdf and _pdf_has_clean_log(output, self.layout.temp, started)
                    else:
                        complete_pdf = True
                    if steady >= 2 and complete_pdf:
                        return output
                    last_size = details.st_size
            await asyncio.sleep(0.1)
        raise LyXMCPError("EXPORT_TIMEOUT", f"LyX did not produce {format} output; {self.runtime._stderr_tail()}")

    async def get_document_state(self, path: str) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            await self._refresh_if_changed(target)
            await self._ensure_buffer(target)
            state = header(target)
            digest = fingerprint(target)
            self._known[target] = digest
            self._conflicted.discard(target)
            return {
                "path": str(target),
                "sha256": digest,
                "tracking_changes": state.tracking_changes,
                "output_changes": state.output_changes,
                "master": state.master,
                "autosave_exists": target.with_name(f"#{target.name}#").exists(),
            }

    async def read(self, path: str, format: Literal["text", "latex"] = "text") -> dict[str, str]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            await self._refresh_if_changed(target)
            result = await self._export(target, format)
            self._known[target] = fingerprint(target)
            self._conflicted.discard(target)
            return {"path": str(target), "format": format, "content": result.read_text(errors="replace")}

    async def export(
        self, path: str, format: ExportFormat = "pdf2", output_path: str | None = None
    ) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            await self._refresh_if_changed(target)
            result = await self._export(target, format)
            self._known[target] = fingerprint(target)
            if output_path:
                destination = Path(output_path).expanduser().absolute()
                parent = destination.parent.resolve(strict=True)
                if not any(parent.is_relative_to(root) for root in self.config.allowed_roots):
                    raise LyXMCPError("PATH_NOT_ALLOWED", "export destination is outside allowed_roots")
                if destination.exists() or destination.is_symlink():
                    raise LyXMCPError("OUTPUT_EXISTS", f"export destination already exists: {destination}")
                if destination.suffix != SUFFIX[format]:
                    raise LyXMCPError("INVALID_ARGUMENT", f"output_path must end in {SUFFIX[format]}")
                with result.open("rb") as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                result = destination
            return {"path": str(result), "format": format, "size": result.stat().st_size}

    async def _run_checker(self, profile: str, path: Path, master: Path) -> dict[str, object]:
        spec = self.config.checker_profiles.get(profile)
        if spec is None:
            raise LyXMCPError("UNKNOWN_CHECKER", f"checker profile is not configured: {profile}")
        command = [part.format(path=str(path), master_path=str(master)) for part in spec.command]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), spec.timeout_sec)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise LyXMCPError("CHECKER_TIMEOUT", f"checker {profile} timed out") from exc
        result: dict[str, object] = {
            "profile": profile,
            "exit_code": process.returncode,
            "output": output.decode(errors="replace")[-8000:],
        }
        if process.returncode:
            raise LyXMCPError("CHECKER_FAILED", f"checker {profile}: {result['output']}")
        return result

    async def _enable_revision_output(self, path: Path) -> None:
        await self._ensure_buffer(path)
        if not header(path).output_changes:
            await self.client.call("changes-output")
        await self.client.call("buffer-write", "force")
        if not header(path).output_changes:
            raise LyXMCPError("OUTPUT_CHANGES_DISABLED", "LyX failed to enable changes in output")

    async def validate_revision(
        self, path: str, master_path: str | None = None, checker_profile: str | None = None
    ) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            master = authorize(master_path, self.config.allowed_roots) if master_path else target
            await self._refresh_if_changed(target)
            if master != target:
                await self._refresh_if_changed(master)
            await self._ensure_buffer(target)
            state = header(target)
            if not state.tracking_changes:
                raise LyXMCPError("TRACKING_DISABLED", "Track Changes is disabled")
            if not state.output_changes:
                raise LyXMCPError("OUTPUT_CHANGES_DISABLED", "Show changes in output is disabled")
            pdf = await self._export(master, "pdf2")
            checker = await self._run_checker(checker_profile, target, master) if checker_profile else None
            self._known[target] = fingerprint(target)
            self._known[master] = fingerprint(master)
            return {
                "ok": True,
                "path": str(target),
                "sha256": fingerprint(target),
                "pdf": str(pdf),
                "pdf_size": pdf.stat().st_size,
                "checker": checker,
            }

    async def apply_edits(
        self,
        path: str,
        edits: list[EditSpec],
        master_path: str | None = None,
        compile: bool = True,
        checker_profile: str | None = None,
    ) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            master = authorize(master_path, self.config.allowed_roots) if master_path else target
            if target in self._conflicted:
                raise ConflictError("CONFLICT_NEEDS_REVIEW", "read the document again after the external modification")
            if checker_profile and checker_profile not in self.config.checker_profiles:
                raise LyXMCPError("UNKNOWN_CHECKER", f"checker profile is not configured: {checker_profile}")
            reject_newer_autosave(target)
            before = fingerprint(target)
            if target in self._known:
                assert_unchanged(target, self._known[target])
            await self._ensure_buffer(target)
            text_file = await self._export(target, "text")
            baseline = text_file.read_text(errors="replace")
            ordered, expected_text = plan(baseline, edits)
            validate_source_spans(target.read_text(errors="replace"), baseline, ordered)
            saved = snapshot(target, self.layout.snapshots)
            initial_header = header(target)
            citation_count = target.read_bytes().count(b"\\begin_inset CommandInset citation")
            expected_disk = before
            stage = "tracking"
            try:
                if not initial_header.tracking_changes:
                    await self.client.call("changes-track")
                    await self.client.call("buffer-write", "force")
                    if not header(target).tracking_changes:
                        raise LyXMCPError("TRACKING_DISABLED", "LyX failed to enable Track Changes")
                    expected_disk = fingerprint(target)
                stage = "revision_output"
                await self._enable_revision_output(target)
                expected_disk = fingerprint(target)
                stage = "edit"
                await self._ensure_buffer(target)
                await apply(self.client, ordered)
                assert_unchanged(target, expected_disk)
                stage = "save"
                await self._ensure_buffer(target)
                await self.client.call("buffer-write", "force")
                expected_disk = fingerprint(target)
                if not header(target).tracking_changes:
                    raise LyXMCPError("TRACKING_DISABLED", "Track Changes was disabled after edit")
                if not header(target).output_changes:
                    raise LyXMCPError("OUTPUT_CHANGES_DISABLED", "LyX disabled changes in output")
                if target.read_bytes().count(b"\\begin_inset CommandInset citation") != citation_count:
                    raise LyXMCPError("CITATION_CHANGED", "plain-text editing changed citation insets")
                stage = "postcondition"
                actual_file = await self._export(target, "text")
                actual = actual_file.read_text(errors="replace")
                if actual != expected_text:
                    raise LyXMCPError("POSTCONDITION_FAILED", "exported text differs from planned edits")
                stage = "compile"
                if master != target:
                    await self._refresh_if_changed(master)
                pdf = await self._export(master, "pdf2") if compile else None
                stage = "checker"
                checker = await self._run_checker(checker_profile, target, master) if checker_profile else None
                after = fingerprint(target)
                self._known[target] = after
                if master != target:
                    self._known[master] = fingerprint(master)
                return {
                    "ok": True,
                    "path": str(target),
                    "sha256_before": before,
                    "sha256_after": after,
                    "tracking_changes": True,
                    "output_changes": True,
                    "edits": [{"index": edit.index, "search_occurrence": edit.search_occurrence} for edit in ordered],
                    "pdf": str(pdf) if pdf else None,
                    "checker": checker,
                    "rolled_back": False,
                }
            except (LyXMCPError, OSError) as exc:
                rolled_back = False
                rollback_verified = False
                rollback_error = None
                try:
                    if isinstance(exc, ConflictError):
                        self._conflicted.add(target)
                    else:
                        assert_unchanged(target, expected_disk)
                        restore(target, saved)
                        rolled_back = True
                    buffer_was_open = target in self._opened
                    await self._ensure_buffer(target)
                    if buffer_was_open:
                        await self.client.call("buffer-reload", "dump")
                    rollback_verified = rolled_back and fingerprint(target) == before
                    if rolled_back:
                        self._known[target] = before
                except (LyXMCPError, OSError) as failed:
                    rollback_error = str(failed)
                    await self.runtime.close(preserve=True)
                return {
                    "ok": False,
                    "stage": stage,
                    "error_code": exc.code if isinstance(exc, LyXMCPError) else type(exc).__name__,
                    "diagnostic": str(exc),
                    "rolled_back": rolled_back,
                    "rollback_verified": rollback_verified,
                    "rollback_error": rollback_error,
                }

    async def import_revision_range(
        self,
        path: str,
        source_path: str,
        start_heading: str,
        end_heading: str,
        expected_sha256: str,
        source_sha256: str,
        compile: bool = True,
    ) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            source = authorize(source_path, self.config.allowed_roots)
            if source == target:
                raise LyXMCPError("INVALID_ARGUMENT", "source and target must differ")
            reject_newer_autosave(target)
            assert_unchanged(target, expected_sha256)
            assert_unchanged(source, source_sha256)
            if target in self._known:
                assert_unchanged(target, self._known[target])
            original = target.read_bytes()
            merged, changes = import_tracked_range(
                original, source.read_bytes(), start_heading, end_heading
            )
            before = fingerprint(target)
            initial_header = header(target)
            await self._ensure_buffer(target)
            saved = snapshot(target, self.layout.snapshots)
            expected_disk = before
            stage = "import"
            try:
                await self.client.call("buffer-close")
                self._opened.discard(target)
                write_bytes_atomically(target, merged)
                expected_disk = fingerprint(target)
                await self._ensure_buffer(target)
                if not initial_header.tracking_changes:
                    stage = "tracking"
                    await self.client.call("changes-track")
                    await self.client.call("buffer-write", "force")
                    expected_disk = fingerprint(target)
                stage = "revision_output"
                await self._enable_revision_output(target)
                expected_disk = fingerprint(target)
                state = header(target)
                if not state.tracking_changes or not state.output_changes:
                    raise LyXMCPError("HEADER_CHANGED", "LyX did not preserve revision settings")
                written = target.read_bytes()
                start, end = section_span(written, start_heading, end_heading)
                actual_changes = len(re.findall(rb"(?m)^\\change_(?:inserted|deleted) ", written[start:end]))
                if actual_changes < changes:
                    raise LyXMCPError("TRACKING_LOST", "LyX removed imported tracked changes")
                stage = "compile"
                pdf = await self._export(target, "pdf2") if compile else None
                after = fingerprint(target)
                self._known[target] = after
                return {
                    "ok": True,
                    "path": str(target),
                    "sha256_before": before,
                    "sha256_after": after,
                    "imported_change_markers": changes,
                    "tracking_changes": True,
                    "output_changes": True,
                    "pdf": str(pdf) if pdf else None,
                    "rolled_back": False,
                }
            except (LyXMCPError, OSError) as exc:
                rolled_back = False
                rollback_verified = False
                rollback_error = None
                try:
                    assert_unchanged(target, expected_disk)
                    if target in self._opened:
                        await self._ensure_buffer(target)
                        await self.client.call("buffer-close")
                        self._opened.discard(target)
                    restore(target, saved)
                    rolled_back = True
                    await self._ensure_buffer(target)
                    rollback_verified = fingerprint(target) == before
                    self._known[target] = before
                except (LyXMCPError, OSError) as failed:
                    rollback_error = str(failed)
                    await self.runtime.close(preserve=True)
                return {
                    "ok": False,
                    "stage": stage,
                    "error_code": exc.code if isinstance(exc, LyXMCPError) else type(exc).__name__,
                    "diagnostic": str(exc),
                    "rolled_back": rolled_back,
                    "rollback_verified": rollback_verified,
                    "rollback_error": rollback_error,
                }

    async def normalize_revisions(
        self, path: str, expected_sha256: str, author_id: int, timestamp: int, compile: bool = True
    ) -> dict[str, object]:
        async with self.lock:
            target = authorize(path, self.config.allowed_roots)
            reject_newer_autosave(target)
            assert_unchanged(target, expected_sha256)
            if target in self._known:
                assert_unchanged(target, self._known[target])
            original = target.read_bytes()
            normalized, merged_groups, accepted_ert = normalize_revision_markup(original, author_id, timestamp)
            if not merged_groups and not accepted_ert:
                raise LyXMCPError("NO_MARKUP_TO_NORMALIZE", "no matching revisions need normalization")
            await self._ensure_buffer(target)
            baseline = (await self._export(target, "text")).read_text(errors="replace")
            saved = snapshot(target, self.layout.snapshots)
            expected_disk = expected_sha256
            stage = "normalize"
            try:
                await self.client.call("buffer-close")
                self._opened.discard(target)
                write_bytes_atomically(target, normalized)
                expected_disk = fingerprint(target)
                await self._ensure_buffer(target)
                await self._enable_revision_output(target)
                expected_disk = fingerprint(target)
                if not header(target).tracking_changes:
                    raise LyXMCPError("TRACKING_DISABLED", "Track Changes was disabled")
                if target.read_bytes().count(b"\\begin_inset CommandInset citation") != original.count(
                    b"\\begin_inset CommandInset citation"
                ):
                    raise LyXMCPError("CITATION_CHANGED", "citation inset count changed")
                stage = "postcondition"
                actual = (await self._export(target, "text")).read_text(errors="replace")
                if actual != baseline:
                    raise LyXMCPError("POSTCONDITION_FAILED", "accepted text changed during normalization")
                stage = "compile"
                pdf = await self._export(target, "pdf2") if compile else None
                after = fingerprint(target)
                self._known[target] = after
                return {
                    "ok": True,
                    "path": str(target),
                    "sha256_before": expected_sha256,
                    "sha256_after": after,
                    "merged_groups": merged_groups,
                    "accepted_structural_ert_replacements": accepted_ert,
                    "output_changes": True,
                    "pdf": str(pdf) if pdf else None,
                    "rolled_back": False,
                }
            except (LyXMCPError, OSError) as exc:
                rollback_error = None
                rolled_back = False
                try:
                    assert_unchanged(target, expected_disk)
                    if target in self._opened:
                        await self._ensure_buffer(target)
                        await self.client.call("buffer-close")
                        self._opened.discard(target)
                    restore(target, saved)
                    rolled_back = True
                    await self._ensure_buffer(target)
                    self._known[target] = expected_sha256
                except (LyXMCPError, OSError) as failed:
                    rollback_error = str(failed)
                    await self.runtime.close(preserve=True)
                return {
                    "ok": False,
                    "stage": stage,
                    "error_code": exc.code if isinstance(exc, LyXMCPError) else type(exc).__name__,
                    "diagnostic": str(exc),
                    "rolled_back": rolled_back,
                    "rollback_verified": rolled_back and fingerprint(target) == expected_sha256,
                    "rollback_error": rollback_error,
                }
