from __future__ import annotations

import asyncio
import difflib
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from .builds import Build, Builds, artifact_content, dependencies, fresh, undefined
from .client import LyXClient
from .compare import compare_documents
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
from .edit import EditSpec, apply, locate_target, plan, validate_source_span, validate_source_spans
from .errors import ConflictError, LyXMCPError
from .index import DocumentIndex
from .jobs import Jobs, stage, transaction_status
from .revision import normalize_revision_markup
from .runtime import Layout, Runtime
from .transaction import TransactionPlan

ExportFormat = Literal["text", "latex", "pdf2"]
SUFFIX = {"text": ".txt", "latex": ".tex", "pdf2": ".pdf"}


def _signature_difference(expected: list[tuple[str, str]], observed: list[tuple[str, str]]) -> str:
    for number in range(max(len(expected), len(observed))):
        before = expected[number] if number < len(expected) else None
        after = observed[number] if number < len(observed) else None
        if before == after:
            continue
        left, right = repr(before), repr(after)
        offset = next(
            (i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b), min(len(left), len(right))
        )
        start, end = max(0, offset - 80), offset + 180
        return f"paragraph {number}, offset {offset}: expected {left[start:end]!r}; observed {right[start:end]!r}"
    return "no difference"


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
        citations = undefined(content, "Citation")
        if citations:
            raise LyXMCPError("UNDEFINED_CITATIONS", ", ".join(sorted(set(citations))))
        references = undefined(content, "Reference")
        if references:
            raise LyXMCPError("UNDEFINED_REFERENCES", ", ".join(sorted(set(references))))
        if "There were undefined references" in content or "There were undefined citations" in content:
            raise LyXMCPError("UNDEFINED_REFERENCES", "compilation reports unresolved citations or references")
        if "Output written on" in content:
            return True
    return False


def _tracked_citation_count(source: bytes, keys: str) -> int:
    pattern = (
        rb'(?m)^\\begin_inset CommandInset citation\r?\nLatexCommand [^\r\n]+\r?\nkey "'
        + re.escape(keys.encode())
        + rb'"'
    )
    count = 0
    for match in re.finditer(pattern, source):
        marker = source.rfind(b"\\change_", 0, match.start())
        if marker >= 0 and source[marker:].startswith(b"\\change_inserted "):
            count += 1
    return count


def _citation_key_lists(source: bytes) -> list[tuple[str, ...]]:
    pattern = rb'(?m)^\\begin_inset CommandInset citation\r?\nLatexCommand [^\r\n]+\r?\nkey "([^"]+)"'
    return [tuple(value.decode().split(",")) for value in re.findall(pattern, source)]


class LyXService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runtime = Runtime(config)
        self.lock = asyncio.Lock()
        self._known: dict[Path, str] = {}
        self._opened: set[Path] = set()
        self._conflicted: set[Path] = set()
        self._indexes: dict[Path, DocumentIndex] = {}
        self.builds = Builds()
        self.jobs = Jobs(self._recover)
        self._comparisons: dict[str, dict[str, Any]] = {}
        self._pending_reload: set[Path] = set()

    async def start(self) -> None:
        await self.runtime.start()

    async def close(self) -> None:
        await self.jobs.close()
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
        if self.runtime.client is None:
            stage("startup")
            await self.runtime.start()
        current = await self.client.call("server-get-filename")
        if current and Path(current).resolve() == path:
            if path in self._pending_reload:
                await self.client.call("buffer-reload", "dump")
                self._pending_reload.discard(path)
            return
        if path in self._opened:
            await self.client.call("buffer-switch", str(path))
        else:
            await self.client.call("file-open", str(path))
            self._opened.add(path)
        current = await self.client.call("server-get-filename")
        if not current or Path(current).resolve() != path:
            raise LyXMCPError("WRONG_BUFFER", f"LyX opened {current!r} instead of {path}")
        if path in self._pending_reload:
            await self.client.call("buffer-reload", "dump")
            self._pending_reload.discard(path)

    async def _refresh_if_changed(self, path: Path) -> None:
        known = self._known.get(path)
        if known is None or fingerprint(path) == known:
            return
        await self._ensure_buffer(path)
        await self.client.call("buffer-reload", "dump")
        self._known[path] = fingerprint(path)

    async def _export(self, path: Path, format: ExportFormat) -> Path:
        await self._ensure_buffer(path)
        stage(f"export_{format}")
        output = self.layout.exports / f"{uuid.uuid4().hex}{SUFFIX[format]}"
        if format == "pdf2":
            await self.client.call("buffer-reset-export")
        started = time.time_ns()
        deadline = time.monotonic() + self.config.export_timeout_sec
        await self.client.call("buffer-export", f"{format} {output}", timeout=self.config.export_timeout_sec)
        last_size = -1
        steady = 0
        while time.monotonic() < deadline:
            if self.runtime.process is None or self.runtime.process.poll() is not None:
                raise LyXMCPError("LYX_EXITED", "LyX exited during export")
            if format == "pdf2":
                try:
                    _raise_fresh_pdf_errors(self.layout.temp, started)
                except LyXMCPError:
                    await self.runtime.abort()
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
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
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

    async def _rollback_buffer_edit(
        self, target: Path, saved: Path, before: str, expected_disk: str, exc: LyXMCPError | OSError
    ) -> dict[str, object]:
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
            "rolled_back": rolled_back,
            "rollback_verified": rollback_verified,
            "rollback_error": rollback_error,
        }

    async def _recover(self) -> dict[str, Any]:
        async with self.lock:
            try:
                await self.runtime.abort()
            except (OSError, TimeoutError) as exc:
                self._conflicted.update(self._known)
                return {"status": "failed", "diagnostic": str(exc), "retry_safe": False}
            self._opened.clear()
            return {"status": "session_stopped", "next_operation": "fresh_isolated_session"}

    def _index(self, target: Path) -> DocumentIndex:
        digest = fingerprint(target)
        cached = self._indexes.get(target)
        if cached is None or cached.version != digest:
            cached = DocumentIndex(target.read_bytes().decode())
            self._indexes[target] = cached
        return cached

    def _state(self, target: Path) -> dict[str, Any]:
        state = header(target)
        return {
            "path": str(target),
            "sha256": fingerprint(target),
            "tracking_changes": state.tracking_changes,
            "output_changes": state.output_changes,
            "master": state.master,
            "autosave_exists": target.with_name(f"#{target.name}#").exists(),
        }

    def _destination(self, output_path: str, suffix: str) -> Path:
        destination = Path(output_path).expanduser().absolute()
        parent = destination.parent.resolve(strict=True)
        if not any(parent.is_relative_to(root) for root in self.config.allowed_roots):
            raise LyXMCPError("PATH_NOT_ALLOWED", "export destination is outside allowed_roots")
        destination = parent / destination.name
        if destination.exists() or destination.is_symlink():
            raise LyXMCPError("OUTPUT_EXISTS", f"export destination already exists: {destination}")
        if destination.suffix != suffix:
            raise LyXMCPError("INVALID_ARGUMENT", f"output_path must end in {suffix}")
        return destination

    async def _build(self, path: Path) -> tuple[Build, bool]:
        version = fingerprint(path)
        cached = self.builds.matching(path, version)
        if cached:
            return cached, True
        baseline = dependencies(path)
        pdf = await self._export(path, "pdf2")
        if not fresh(baseline):
            raise ConflictError("BUILD_INPUT_CHANGED", "source or dependency changed during compilation")
        return self.builds.retain(path, pdf, self.layout.temp, self.layout.exports, version), False

    async def read_document(
        self,
        path: str,
        selector: dict[str, Any] | None = None,
        search: str | None = None,
        representations: list[str] | None = None,
        max_chars: int = 12000,
        format: Literal["text", "latex"] | None = None,
        build: bool = False,
        build_id: str | None = None,
        artifact: str | None = None,
        output_path: str | None = None,
        content_offset: int = 0,
        content_limit: int = 4000,
        validate: bool = False,
        pdf: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            stage("read_index")
            target = authorize(path, self.config.allowed_roots)
            if not 1000 <= max_chars <= 50000 or content_offset < 0 or not 0 <= content_limit <= 50000:
                raise LyXMCPError(
                    "INVALID_ARGUMENT", "max_chars must be 1000..50000; content offsets/limits must be bounded"
                )
            current_sha = fingerprint(target)
            if target in self._opened and self._known.get(target) != current_sha:
                self._pending_reload.add(target)
            self._known[target] = current_sha
            self._conflicted.discard(target)
            if pdf is not None:
                from .pdf import inspect_pdf

                if self.runtime.layout is None:
                    self.runtime._prepare_layout()
                return await asyncio.to_thread(
                    inspect_pdf, pdf, self.config.allowed_roots, self.layout.exports, self._index(target)
                )
            retained = self.builds.get(build_id) if build_id else None
            reused = False
            if retained and retained.path != target:
                raise LyXMCPError("BUILD_DOCUMENT_MISMATCH", "build belongs to a different document")
            if validate:
                state = header(target)
                if not state.tracking_changes or not state.output_changes:
                    raise LyXMCPError(
                        "REVISION_SETTINGS_DISABLED", "tracking_changes and output_changes must be enabled"
                    )
            if build or validate:
                retained, reused = await self._build(target)
            if artifact:
                if retained is None:
                    retained, reused = await self._build(target)
                result = artifact_content(retained, artifact, content_offset, min(content_limit, max_chars - 800))
                result.update(
                    {
                        "build_id": retained.id,
                        "reused": reused,
                        "current": retained.version == fingerprint(target) and fresh(retained.dependencies),
                    }
                )
                if output_path:
                    original = Path(str(result["path"]))
                    destination = self._destination(output_path, original.suffix)
                    with original.open("rb") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    result["output_path"] = str(destination)
                return result
            index = self._index(target)
            reps = representations or ["text", "nodes"]
            if set(reps) - {"text", "source", "nodes"}:
                raise LyXMCPError(
                    "INVALID_REPRESENTATION", "representations are text/source/nodes; native export uses format"
                )
            result = {
                "ok": True,
                "state": self._state(target),
                "document_version": index.version,
                "outline": [],
                "outline_total": len(index.outline()),
                "passages": [],
                "truncated": False,
            }
            for item in index.outline():
                item["title"] = str(item["title"])[:200]
                proposed = {**result, "outline": [*result["outline"], item]}
                if len(json.dumps(proposed, ensure_ascii=False)) > max_chars // 3:
                    result["truncated"] = True
                    break
                result["outline"].append(item)
            selected = index.select(selector, search)
            for node in selected:
                passage = index.describe(node, reps)
                for key in ("text", "source"):
                    if key in passage:
                        value = passage[key]
                        passage[key] = value[content_offset : content_offset + content_limit]
                        passage[key + "_total_chars"] = len(value)
                        passage[key + "_truncated"] = content_offset + content_limit < len(value)
                proposed = {**result, "passages": [*result["passages"], passage]}
                if len(json.dumps(proposed, ensure_ascii=False)) > max_chars - 500:
                    result["truncated"] = True
                    result["next_node_id"] = node.id
                    break
                result["passages"].append(passage)
            result["returned_passages"] = len(result["passages"])
            result["next_offset"] = (selector or {}).get("offset", 0) + len(result["passages"])
            if format:
                if selector or search:
                    raise LyXMCPError(
                        "INVALID_ARGUMENT", "native exports are whole-document; use scoped text/source/nodes"
                    )
                await self._refresh_if_changed(target)
                cached_build = retained or self.builds.matching(target, index.version)
                if format == "latex" and cached_build and "latex" in cached_build.artifacts:
                    exported = Path(cached_build.artifacts["latex"]["path"])
                    retained, reused = cached_build, True
                else:
                    exported = await self._export(target, format)
                content = exported.read_text(errors="replace")
                result = {
                    "ok": True,
                    "state": self._state(target),
                    "format": format,
                    "content": content[content_offset : content_offset + min(content_limit, max_chars - 800)],
                    "total_chars": len(content),
                    "offset": content_offset,
                    "truncated": content_offset + min(content_limit, max_chars - 800) < len(content),
                    "artifact": {"path": str(exported), "size": exported.stat().st_size},
                }
            if retained:
                result["build"] = retained.describe(reused)
            return result

    async def compare(
        self,
        path: str,
        revised_path: str,
        base_path: str | None = None,
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 12000,
    ) -> dict[str, Any]:
        async with self.lock:
            stage("compare")
            target = authorize(path, self.config.allowed_roots)
            revised = authorize(revised_path, self.config.allowed_roots)
            base = authorize(base_path, self.config.allowed_roots) if base_path else target
            if offset < 0 or not 0 <= limit <= 100 or not 1000 <= max_chars <= 50000:
                raise LyXMCPError("INVALID_ARGUMENT", "comparison offsets and output limits must be bounded")
            comparison = await asyncio.to_thread(
                compare_documents, self._index(base), self._index(revised), self._index(target)
            )
            self._comparisons[comparison["compare_id"]] = comparison
            result = {**comparison, "hunks": [], "total_hunks": len(comparison["hunks"])}
            for hunk in comparison["hunks"][offset : offset + limit]:
                summary = {key: value for key, value in hunk.items() if key != "operation"}
                for key in ("before", "after"):
                    if len(summary.get(key, "")) > 2000:
                        summary[key] = summary[key][:2000]
                        summary["content_truncated"] = True
                proposed = {**result, "hunks": [*result["hunks"], summary]}
                if len(json.dumps(proposed, ensure_ascii=False)) > max_chars - 300:
                    break
                result["hunks"].append(summary)
            result["next_offset"] = offset + len(result["hunks"])
            result["truncated"] = result["next_offset"] < len(comparison["hunks"])
            return result

    def _expand_hunks(self, operations: list[dict[str, Any]], version: str) -> list[dict[str, Any]]:
        result = []
        for operation in operations:
            if operation.get("op") != "apply_hunk":
                result.append(operation)
                continue
            comparison = self._comparisons.get(operation.get("compare_id", ""))
            if comparison is None or comparison["expected_sha256"] != version:
                result.append({**operation, "op": "stale_comparison"})
                continue
            hunk = next((h for h in comparison["hunks"] if h["id"] == operation.get("hunk_id")), None)
            if hunk is None or hunk["conflict"] or "operation" not in hunk:
                result.append({**operation, "op": "conflicting_hunk"})
                continue
            result.append({**hunk["operation"], "id": operation.get("id")})
        return result

    async def transact(
        self,
        path: str,
        expected_sha256: str,
        operations: list[dict[str, Any]],
        protect: list[dict[str, str]] | None = None,
        validation: dict[str, Any] | None = None,
        assertions: dict[str, Any] | None = None,
        output_path: str | None = None,
        master_path: str | None = None,
        checker_profile: str | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            stage("preflight")
            target = authorize(path, self.config.allowed_roots)
            transaction_status("not_started")
            if not expected_sha256:
                raise LyXMCPError("VERSION_REQUIRED", "every mutation requires expected_sha256 from lyx_read")
            assert_unchanged(target, expected_sha256)
            reject_newer_autosave(target)
            if target in self._conflicted:
                raise ConflictError("CONFLICT_NEEDS_REVIEW", "read the document before retrying")
            if not 1 <= len(operations) <= 200:
                raise LyXMCPError("INVALID_ARGUMENT", "transactions require 1..200 operations")
            validation = validation or {}
            if set(validation) - {"compile", "visible_revisions", "preserve_unedited_insets", "no_unresolved"}:
                raise LyXMCPError("INVALID_VALIDATION", "unknown validation option")
            if validation.get("no_unresolved") and not validation.get("compile", True):
                raise LyXMCPError("INVALID_VALIDATION", "no_unresolved requires compilation")
            if assertions and set(assertions) - {"unchanged_insets", "only_regions", "tracking_enabled"}:
                raise LyXMCPError("INVALID_ASSERTION", "unknown transaction assertion")
            if checker_profile and checker_profile not in self.config.checker_profiles:
                raise LyXMCPError("UNKNOWN_CHECKER", f"checker profile is not configured: {checker_profile}")
            destination = self._destination(output_path, ".pdf") if output_path else None
            if destination and not validation.get("compile", True):
                raise LyXMCPError("INVALID_VALIDATION", "PDF delivery requires compile=true")
            index = self._index(target)
            planned = TransactionPlan(index, self.config.allowed_roots)
            expanded = self._expand_hunks(operations, expected_sha256)
            errors = planned.preflight(expanded, protect or [], assertions or {})
            if errors:
                return {
                    "ok": False,
                    "stage": "preflight",
                    "errors": errors,
                    "transaction_status": "not_started",
                    "sha256": expected_sha256,
                    "retry_safe": True,
                }
            candidate = planned.render()
            expected = DocumentIndex(candidate)
            if validation.get("preserve_unedited_insets", True):
                remaining = iter(expected.inset_signature(n) for n in expected.insets if expected.active(n))
                for node in index.insets:
                    if node.id in planned.changed_insets or not index.active(node):
                        continue
                    signature = index.inset_signature(node)
                    if not any(value == signature for value in remaining):
                        raise LyXMCPError("INSET_PRESERVATION", f"unchanged inset content/order was altered: {node.id}")
            baseline_dependencies = dependencies(target)
            master = authorize(master_path, self.config.allowed_roots) if master_path else target
            if master != target:
                baseline_dependencies.update(dependencies(master))
            temporary: list[Path] = []
            delivered = False
            committed = False
            transaction_status("not_committed")
            try:
                stage("candidate")
                with tempfile.NamedTemporaryFile(
                    prefix="lyx-mcp-", suffix=".lyx", dir=target.parent, delete=False
                ) as file:
                    draft = Path(file.name)
                    file.write(candidate.encode())
                temporary.append(draft)
                await self._ensure_buffer(draft)
                stage("save_candidate")
                await self.client.call("buffer-write", "force")
                actual = DocumentIndex(draft.read_bytes().decode())
                stage("verify")
                wanted, observed = expected.signature(), actual.signature()
                if observed != wanted:
                    raise LyXMCPError(
                        "POSTCONDITION_FAILED",
                        "LyX changed planned content/properties: " + _signature_difference(wanted, observed),
                    )
                if actual.rejected_signature() != expected.rejected_signature():
                    raise LyXMCPError(
                        "REVISION_PRESERVATION_FAILED", "LyX changed the planned rejected revision content"
                    )
                if actual.revision_signature() != expected.revision_signature():
                    raise LyXMCPError(
                        "REVISION_PRESERVATION_FAILED", "LyX changed the planned revision status or attribution"
                    )
                state = header(draft)
                if not state.tracking_changes or not state.output_changes:
                    raise LyXMCPError("TRACKING_LOST", "LyX did not preserve revision settings")
                compile_target = draft
                if master != target:
                    with tempfile.NamedTemporaryFile(
                        prefix="lyx-mcp-master-", suffix=".lyx", dir=master.parent, delete=False
                    ) as file:
                        compile_target = Path(file.name)
                        temporary.append(compile_target)
                        master_source = master.read_text()
                        included: list[bool] = []

                        def replace_child(match: re.Match[str]) -> str:
                            name = match[1].strip().strip('"')
                            child = (master.parent / name).resolve()
                            if child == target:
                                included.append(True)
                                return f'filename "{draft}"'
                            return match[0]

                        master_source = re.sub(r"(?m)^filename (.+)$", replace_child, master_source)
                        if not included:
                            raise LyXMCPError("UNSUPPORTED_MASTER", "master must directly include the edited document")
                        master_source = re.sub(
                            r"(?m)^\\output_changes (?:true|false)", r"\\output_changes true", master_source, count=1
                        )
                        file.write(master_source.encode())
                compiled: Build | None = None
                if validation.get("compile", True):
                    pdf_file = await self._export(compile_target, "pdf2")
                    stage("retain_build")
                    compiled = self.builds.retain(
                        target, pdf_file, self.layout.temp, self.layout.exports, actual.version
                    )
                stage("checker")
                checker = await self._run_checker(checker_profile, draft, compile_target) if checker_profile else None
                for opened in reversed(temporary):
                    if opened in self._opened:
                        await self._ensure_buffer(opened)
                        await self.client.call("buffer-close")
                        self._opened.discard(opened)
                stage("commit")
                if not fresh(baseline_dependencies):
                    self._conflicted.add(target)
                    transaction_status("external_conflict")
                    raise ConflictError("EXTERNAL_MODIFICATION", "document or dependency changed during transaction")
                assert_unchanged(target, expected_sha256)
                if destination and compiled:
                    with Path(compiled.artifacts["pdf"]["path"]).open("rb") as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    delivered = True
                write_bytes_atomically(target, draft.read_bytes())
                committed = True
                transaction_status("committed")
                self._known[target] = fingerprint(target)
                self._indexes[target] = actual
                if target in self._opened:
                    self._pending_reload.add(target)
                if compiled:
                    compiled.dependencies.update(dependencies(target))
                    if master != target:
                        compiled.dependencies.update(dependencies(master))
                    manifest = Path(compiled.artifacts["pdf"]["path"]).parent / "manifest.json"
                    manifest.write_text(json.dumps(compiled.describe(details=True), indent=2))
                changed_passages: list[dict[str, Any]] = []
                before_passages = index.signature(index.top)
                after_passages = actual.signature(actual.top)
                for tag, _, _, left, right in difflib.SequenceMatcher(
                    None, before_passages, after_passages, autojunk=False
                ).get_opcodes():
                    if tag != "equal":
                        changed_passages.extend(
                            {"id": p.id, "text": actual.text(p)[:1000], "truncated": len(actual.text(p)) > 1000}
                            for p in actual.top[left:right]
                        )
                changed_passages = changed_passages[:20]
                return {
                    "ok": True,
                    "state": self._state(target),
                    "sha256_before": expected_sha256,
                    "sha256_after": actual.version,
                    "transaction_status": "committed",
                    "retry_safe": False,
                    "operations": planned.results,
                    "changed_passages": changed_passages,
                    "validation": {
                        "content_verified": True,
                        "revisions_verified": True,
                        "tracking_changes": True,
                        "output_changes": True,
                        "compiled": compiled is not None,
                        "checker": checker,
                    },
                    "build": compiled.describe() if compiled else None,
                    "output_path": str(destination) if destination else None,
                }
            except BaseException:
                if not committed:
                    if delivered and destination:
                        destination.unlink(missing_ok=True)
                    if target.exists() and fingerprint(target) == expected_sha256:
                        transaction_status("rolled_back")
                    else:
                        self._conflicted.add(target)
                        transaction_status("external_conflict")
                raise
            finally:
                for file_path in temporary:
                    file_path.unlink(missing_ok=True)

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
                return {
                    "ok": False,
                    "stage": stage,
                    "error_code": exc.code if isinstance(exc, LyXMCPError) else type(exc).__name__,
                    "diagnostic": str(exc),
                    **await self._rollback_buffer_edit(target, saved, before, expected_disk, exc),
                }

    async def insert_citation(
        self,
        path: str,
        anchor: str,
        keys: str,
        position: Literal["before", "after"] = "after",
        occurrence: int = 1,
        require_unique: bool = True,
        context_before: str | None = None,
        context_after: str | None = None,
        master_path: str | None = None,
        compile: bool = True,
        checker_profile: str | None = None,
    ) -> dict[str, object]:
        if not anchor or len(anchor.encode()) > 2000 or any(character in anchor for character in "\r\n\x00"):
            raise LyXMCPError("INVALID_ARGUMENT", "anchor must be nonempty single-line text of at most 2000 bytes")
        if occurrence < 1 or position not in ("before", "after"):
            raise LyXMCPError("INVALID_ARGUMENT", "invalid citation position or occurrence")
        parts = [part.strip() for part in keys.split(",")]
        if any(
            not part
            or any(not character.isprintable() or character.isspace() or character in '"|{}\\' for character in part)
            for part in parts
        ):
            raise LyXMCPError("INVALID_ARGUMENT", "keys must be comma-separated bibliography keys")
        normalized_keys = ",".join(parts)
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
            baseline = (await self._export(target, "text")).read_text(errors="replace")
            _, _, search_occurrence = locate_target(
                baseline, anchor, occurrence, require_unique, context_before, context_after
            )
            source = target.read_bytes()
            validate_source_span(source.decode(errors="replace"), baseline, anchor)
            citation_count = source.count(b"\\begin_inset CommandInset citation")
            citation_keys = _citation_key_lists(source)
            tracked_count = _tracked_citation_count(source, normalized_keys)
            saved = snapshot(target, self.layout.snapshots)
            initial_header = header(target)
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
                await self.client.call("buffer-begin")
                for _ in range(search_occurrence):
                    response = await self.client.call("word-find-forward", anchor)
                    if response:
                        raise LyXMCPError("TARGET_NOT_FOUND", f"LyX search: {response}")
                await self.client.call("mark-off")
                if position == "before":
                    for _ in anchor:
                        await self.client.call("char-backward")
                missing_keys = await self.client.call("citation-insert", normalized_keys)
                assert_unchanged(target, expected_disk)
                stage = "save"
                await self.client.call("buffer-write", "force")
                expected_disk = fingerprint(target)
                state = header(target)
                if not state.tracking_changes or not state.output_changes:
                    raise LyXMCPError("HEADER_CHANGED", "LyX did not preserve revision settings")
                written = target.read_bytes()
                merged_into_existing = False
                written_count = written.count(b"\\begin_inset CommandInset citation")
                if written_count == citation_count + 1:
                    if _tracked_citation_count(written, normalized_keys) != tracked_count + 1:
                        raise LyXMCPError("TRACKING_LOST", "new citation is not a tracked insertion")
                elif written_count == citation_count:
                    updated_keys = _citation_key_lists(written)
                    if len(updated_keys) != len(citation_keys):
                        raise LyXMCPError("CITATION_NOT_INSERTED", "LyX did not add a citation key")
                    changed = [(old, new) for old, new in zip(citation_keys, updated_keys, strict=True) if old != new]
                    if len(changed) != 1:
                        raise LyXMCPError("CITATION_NOT_INSERTED", "LyX did not add a citation key")
                    old, new = changed[0]
                    expected = set(old) | set(parts)
                    if set(new) != expected or len(new) != len(expected) or len(new) <= len(old):
                        raise LyXMCPError("CITATION_NOT_INSERTED", "LyX did not merge the requested citation keys")
                    merged_into_existing = True
                else:
                    raise LyXMCPError("CITATION_NOT_INSERTED", "LyX changed the citation inset count unexpectedly")
                if missing_keys.strip():
                    raise LyXMCPError("UNDEFINED_CITATIONS", missing_keys)
                stage = "postcondition"
                actual = (await self._export(target, "text")).read_text(errors="replace")
                if actual.count(anchor) != baseline.count(anchor):
                    raise LyXMCPError("POSTCONDITION_FAILED", "citation insertion changed its plain-text anchor")
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
                    "keys": normalized_keys,
                    "merged_into_existing": merged_into_existing,
                    "search_occurrence": search_occurrence,
                    "pdf": str(pdf) if pdf else None,
                    "checker": checker,
                    "rolled_back": False,
                }
            except (LyXMCPError, OSError) as exc:
                return {
                    "ok": False,
                    "stage": stage,
                    "error_code": exc.code if isinstance(exc, LyXMCPError) else type(exc).__name__,
                    "diagnostic": str(exc),
                    **await self._rollback_buffer_edit(target, saved, before, expected_disk, exc),
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
            merged, changes = import_tracked_range(original, source.read_bytes(), start_heading, end_heading)
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
