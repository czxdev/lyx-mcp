from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .document import fingerprint
from .errors import LyXMCPError


def undefined(content: str, kind: str) -> list[str]:
    return re.findall(rf"{kind} [`']([^']+)'(?:(?!\n\n).){{0,2000}}?\bundefined", content, flags=re.S)


def dependencies(path: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    visited: set[Path] = set()
    pending = [path]
    while pending:
        current = pending.pop()
        key = os.path.abspath(current)
        if key in result:
            continue
        if not current.is_file():
            result[key] = None
            continue
        result[key] = fingerprint(current)
        resolved = current.resolve()
        if resolved in visited:
            continue
        visited.add(resolved)
        if current.suffix.lower() not in (".lyx", ".tex", ".sty", ".cls"):
            continue
        source = current.read_text(errors="replace")
        refs: list[tuple[str, str]] = []
        refs += [(m[1].strip().strip('"'), "") for m in re.finditer(r"(?m)^\s*filename (.+)$", source)]
        refs += [
            (name.strip(), ".bib") for m in re.finditer(r'(?m)^bibfiles "([^"]+)"', source) for name in m[1].split(",")
        ]
        refs += [(m[1], ".lyx") for m in re.finditer(r'(?m)^\\master "?([^"\n]+)', source)]
        refs += [
            (m[2], ".bib" if m[1] in ("bibliography", "addbibresource") else ".tex")
            for m in re.finditer(r"\\(input|include|bibliography|addbibresource)(?:\[[^]]*\])?\{([^}]+)\}", source)
        ]
        refs += [(m[1], ".aux") for m in re.finditer(r"\\externaldocument(?:\[[^]]*\])?\{([^}]+)\}", source)]
        refs += [(m[1], ".bst") for m in re.finditer(r'(?m)^options "([^"]+)"', source)]
        for value, suffix in refs:
            if value in ("", "unavailable") or "$$" in value or "\\" in value:
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = current.parent / candidate
            if suffix and not candidate.suffix:
                candidate = candidate.with_suffix(suffix)
            pending.append(candidate)
    return result


def fresh(deps: dict[str, str | None]) -> bool:
    return all((fingerprint(Path(p)) if Path(p).is_file() else None) == sha for p, sha in deps.items())


@dataclass
class Build:
    id: str
    path: Path
    version: str
    dependencies: dict[str, str | None]
    artifacts: dict[str, dict[str, Any]]
    diagnostics: dict[str, Any]
    mappings: dict[str, dict[str, str]]

    def describe(self, reused: bool = False, details: bool = False) -> dict[str, Any]:
        result = {
            "build_id": self.id,
            "sha256": self.version,
            "reused": reused,
            "artifacts": self.artifacts
            if details
            else {key: value for key, value in self.artifacts.items() if key in ("pdf", "latex", "log", "aux", "bbl")},
            "diagnostics": self.diagnostics
            if details
            else {key: value for key, value in self.diagnostics.items() if key != "warnings"},
        }
        if details:
            result["mappings"] = self.mappings
            result["dependencies"] = self.dependencies
        return result


class Builds:
    def __init__(self) -> None:
        self.items: dict[str, Build] = {}

    def matching(self, path: Path, version: str) -> Build | None:
        for build in reversed(list(self.items.values())):
            if build.path == path and build.version == version and fresh(build.dependencies):
                if all(Path(a["path"]).is_file() for a in build.artifacts.values()):
                    return build
        return None

    def get(self, build_id: str) -> Build:
        if build_id not in self.items:
            raise LyXMCPError("UNKNOWN_BUILD", "build ID is absent from this server session")
        return self.items[build_id]

    def retain(self, path: Path, pdf: Path, temp: Path, directory: Path, version: str) -> Build:
        build_id = uuid.uuid4().hex
        root = directory / build_id
        root.mkdir()
        pdf_hash = fingerprint(pdf)
        generated = next(
            (
                p
                for p in sorted(temp.rglob("*.pdf"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
                if p.with_suffix(".log").exists()
                and p.stat().st_size == pdf.stat().st_size
                and fingerprint(p) == pdf_hash
            ),
            None,
        )
        artifacts: dict[str, dict[str, Any]] = {}
        if generated:
            shutil.copytree(generated.parent, root / "outputs")
            for file in sorted((root / "outputs").rglob("*")):
                if not file.is_file():
                    continue
                name = str(file.relative_to(root / "outputs"))
                artifacts[name] = {"path": str(file), "size": file.stat().st_size}
            for kind, suffix in (("latex", ".tex"), ("log", ".log"), ("aux", ".aux"), ("bbl", ".bbl")):
                file = root / "outputs" / generated.with_suffix(suffix).name
                if file.exists():
                    artifacts[kind] = {"path": str(file), "size": file.stat().st_size}
        final_pdf = root / "document.pdf"
        shutil.copy2(pdf, final_pdf)
        artifacts["pdf"] = {"path": str(final_pdf), "size": final_pdf.stat().st_size}
        logs = "\n".join(
            Path(a["path"]).read_text(errors="replace") for name, a in artifacts.items() if name.endswith(".log")
        )
        warnings = re.findall(r"(?:LaTeX|Package [^ ]+) Warning:[^\n]*(?:\n[^\n]*)?", logs)
        unresolved = undefined(logs, "Citation") + undefined(logs, "Reference")
        aux = "\n".join(
            Path(a["path"]).read_text(errors="replace") for name, a in artifacts.items() if name.endswith(".aux")
        )
        mappings = {
            "citations": dict(re.findall(r"\\bibcite\{([^}]+)\}\{([^\n]+)\}", aux)),
            "references": dict(re.findall(r"\\newlabel\{([^}]+)\}\{\{([^}]+)\}", aux)),
        }
        build = Build(
            build_id,
            path,
            version,
            dependencies(path),
            artifacts,
            {
                "warnings": warnings[:20],
                "warning_count": len(warnings),
                "unresolved": sorted(set(unresolved)),
                "compiled": True,
            },
            mappings,
        )
        self.items[build_id] = build
        if generated:
            for depfile in generated.parent.glob("*.dep-pdf"):
                for line in depfile.read_text(errors="replace").splitlines():
                    match = re.fullmatch(r"\d+ \d+ (.+)", line)
                    if not match:
                        continue
                    input_path = Path(match[1])
                    if not input_path.is_absolute():
                        input_path = generated.parent / input_path
                    input_path = input_path.resolve()
                    if input_path.is_file() and not input_path.is_relative_to(temp):
                        build.dependencies[str(input_path)] = fingerprint(input_path)
        (root / "manifest.json").write_text(json.dumps(build.describe(details=True), indent=2))
        return build


def artifact_content(build: Build, name: str, offset: int = 0, limit: int = 12000) -> dict[str, Any]:
    if (name not in build.artifacts and name != "manifest") or offset < 0 or not 0 <= limit <= 50000:
        raise LyXMCPError("INVALID_ARTIFACT", "select a retained artifact; limit must be between 0 and 50000")
    artifact: dict[str, Any]
    if name == "manifest":
        path = Path(build.artifacts["pdf"]["path"]).parent / "manifest.json"
        artifact = {"path": str(path), "size": path.stat().st_size}
    else:
        artifact = build.artifacts[name]
    if Path(artifact["path"]).suffix == ".pdf":
        return dict(artifact)
    content = Path(artifact["path"]).read_text(errors="replace")
    return {
        **artifact,
        "content": content[offset : offset + limit],
        "offset": offset,
        "total_chars": len(content),
        "truncated": offset + limit < len(content),
    }
