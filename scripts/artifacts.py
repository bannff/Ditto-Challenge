"""Canonical, scrubbed evidence bundles for demo runs."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from self_improving_coding_agent.cli import render_replay
from self_improving_coding_agent.contracts import RUN_ID_RE, Block, RunReport
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.scrub import scrub_text

SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 1_000_000
ARTIFACT_FILENAMES = (
    "manifest.json",
    "trace.log",
    "report.json",
    "diff.patch",
    "chain.json",
    "chain.log",
)
MANIFEST_ARTIFACTS = ARTIFACT_FILENAMES[1:]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactDigest(_StrictModel):
    sha256: str
    bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)


class ArtifactManifest(_StrictModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    expected_outcome: str
    artifacts: dict[str, ArtifactDigest]


class ChainHead(_StrictModel):
    length: int = Field(ge=0)
    head_hash: str


class ChainExport(_StrictModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    blocks: list[Block]
    head: ChainHead


CONTRAST_SCHEMA_VERSION = 1


class ContrastRuns(_StrictModel):
    control: str = Field(min_length=1)
    primed: str = Field(min_length=1)


class ContrastAcceptance(_StrictModel):
    control: bool
    primed: bool


class ContrastCheck(_StrictModel):
    label: str = Field(min_length=1)
    control: bool
    primed: bool


class SelfImprovementContrast(_StrictModel):
    schema_version: Literal[1] = CONTRAST_SCHEMA_VERSION
    scenario: str = Field(min_length=1)
    primed_rule: str = Field(min_length=1)
    runs: ContrastRuns
    acceptance: ContrastAcceptance
    check: ContrastCheck


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        text = scrub_text(value)
        return text[:MAX_ARTIFACT_BYTES]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _bounded_text(value: str) -> str:
    return scrub_text(value)[:MAX_ARTIFACT_BYTES]


def _json_bytes(value: BaseModel) -> bytes:
    content = value.model_dump_json(indent=2).encode("utf-8")
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ValueError("canonical artifact exceeds byte limit")
    return content + b"\n"


def safe_output_name(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise ValueError("unsafe output name")
    return value


def _reject_symlink_ancestors(dest: Path) -> None:
    absolute = dest if dest.is_absolute() else Path.cwd() / dest
    for directory in (absolute, *absolute.parents):
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("artifact destination must not traverse a symlink")


def _prepare_destination(dest: Path, *, require_safe_name: bool) -> None:
    if require_safe_name:
        safe_output_name(dest.name)
    _reject_symlink_ancestors(dest)
    try:
        mode = dest.lstat().st_mode
    except FileNotFoundError:
        dest.mkdir(parents=True, exist_ok=False)
        mode = dest.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError("artifact destination must be a real directory")


def _atomic_write(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temp_path = Path(temporary.name)
    os.replace(temp_path, path)


def write_self_improvement_contrast(*, contrast: SelfImprovementContrast, dest: Path) -> Path:
    _prepare_destination(dest, require_safe_name=False)
    clean_contrast = SelfImprovementContrast.model_validate(_clean(contrast.model_dump()))
    path = dest / "contrast.json"
    _atomic_write(path, _json_bytes(clean_contrast))
    return path


def write_run_bundle(*, report: RunReport, ledger: Ledger, dest: Path, trace: str) -> Path:
    """Export a supplied run and its ledger record; this does not run or judge the workflow."""
    head = ledger.head(report.run_id)
    if head is None:
        raise ValueError(f"run {report.run_id} has no ledger head")
    replay = io.StringIO()
    render_replay(ledger, report.run_id, out=replay)
    clean_report = RunReport.model_validate(_clean(report.model_dump(mode="json")))
    chain = ChainExport(
        run_id=report.run_id,
        blocks=ledger.blocks(report.run_id),
        head=ChainHead(length=head[0], head_hash=head[1]),
    )
    contents = {
        "trace.log": _bounded_text(trace).encode("utf-8"),
        "report.json": _json_bytes(clean_report),
        "diff.patch": _bounded_text(report.evidence).encode("utf-8"),
        "chain.json": _json_bytes(chain),
        "chain.log": _bounded_text(replay.getvalue()).encode("utf-8"),
    }
    _prepare_destination(dest, require_safe_name=True)
    for name, content in contents.items():
        _atomic_write(dest / name, content)
    manifest = ArtifactManifest(
        run_id=report.run_id,
        expected_outcome=str(report.outcome),
        artifacts={
            name: ArtifactDigest(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
            for name, content in contents.items()
        },
    )
    _atomic_write(dest / "manifest.json", _json_bytes(manifest))
    return dest
