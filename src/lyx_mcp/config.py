from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CheckerProfile:
    command: tuple[str, ...]
    timeout_sec: float = 120


@dataclass(frozen=True, slots=True)
class Config:
    lyx_binary: str = "lyx"
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    runtime_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    profile_seed_dir: Path | None = None
    startup_timeout_sec: float = 45
    command_timeout_sec: float = 30
    export_timeout_sec: float = 180
    checker_profiles: dict[str, CheckerProfile] = field(default_factory=dict)


def load_config() -> Config:
    path = os.environ.get("LYX_MCP_CONFIG")
    if not path:
        return Config()
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    lyx = data.get("lyx", {})
    security = data.get("security", {})
    checkers = data.get("checker", {})
    roots = tuple(Path(p).expanduser().resolve() for p in security.get("allowed_roots", [os.getcwd()]))
    if not roots:
        raise ValueError("security.allowed_roots must not be empty")
    seed = lyx.get("profile_seed_dir")
    return Config(
        lyx_binary=lyx.get("binary", "lyx"),
        allowed_roots=roots,
        runtime_root=Path(lyx.get("runtime_root", tempfile.gettempdir())).expanduser().resolve(),
        profile_seed_dir=Path(seed).expanduser().resolve() if seed else None,
        startup_timeout_sec=float(lyx.get("startup_timeout_sec", 45)),
        command_timeout_sec=float(lyx.get("command_timeout_sec", 30)),
        export_timeout_sec=float(lyx.get("export_timeout_sec", 180)),
        checker_profiles={
            name: CheckerProfile(tuple(spec["command"]), float(spec.get("timeout_sec", 120)))
            for name, spec in checkers.items()
        },
    )
