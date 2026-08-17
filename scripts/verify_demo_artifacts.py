"""Offline consistency verifier for canonical demo evidence bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from artifacts import (
    ARTIFACT_FILENAMES,
    MAX_ARTIFACT_BYTES,
    SelfImprovementContrast,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from self_improving_coding_agent.contracts import (
    Block,
    BlockType,
    Outcome,
    RunReport,
)
from self_improving_coding_agent.ledger import Ledger

_HEX_DIGEST = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictArtifactDigest(_StrictModel):
    sha256: _HEX_DIGEST
    bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)


class StrictManifest(_StrictModel):
    schema_version: Literal[1]
    run_id: str
    expected_outcome: Outcome
    artifacts: dict[str, StrictArtifactDigest]


class StrictTicket(_StrictModel):
    id: str
    repository: str
    request: str
    domain: str = "general"
    acceptance_command: str | None = None
    created_at: str


class StrictEvaluatorScore(_StrictModel):
    evaluator: str
    score: float
    threshold: float
    passed: bool
    reason: str = ""
    gating: bool = True


class StrictVerdict(_StrictModel):
    schema_version: int
    node: str
    passed: bool
    attempts: int
    scores: list[StrictEvaluatorScore]
    diagnosis: str | None = None


class StrictAcceptance(_StrictModel):
    command: str
    exit_code: int
    output_tail: str = ""
    passed: bool


class StrictLesson(_StrictModel):
    schema_version: int
    ticket_id: str
    outcome: Outcome
    content: str
    tags: list[str]
    created_at: str


class StrictRunReport(_StrictModel):
    schema_version: Literal[1]
    run_id: str
    ticket: StrictTicket
    branch: str | None = None
    worktree: str | None = None
    outcome: Outcome
    # Optional so bundles exported before the field existed still verify.
    summary: str = ""
    verdicts: list[StrictVerdict]
    acceptance: StrictAcceptance | None = None
    evidence: str
    lesson: StrictLesson | None = None
    created_at: str


class StrictBlock(_StrictModel):
    schema_version: int
    run_id: str
    seq: int
    block_type: BlockType
    payload: dict[str, Any]
    git_hash: str | None = None
    prev_hash: _HEX_DIGEST
    content_hash: _HEX_DIGEST
    created_at: str


class StrictChainHead(_StrictModel):
    length: int = Field(ge=0)
    head_hash: _HEX_DIGEST


class StrictChainExport(_StrictModel):
    schema_version: Literal[1]
    run_id: str
    blocks: list[StrictBlock]
    head: StrictChainHead


@contextmanager
def _open_directory(
    path: Path | None = None, *, parent_fd: int | None = None, name: str | None = None
) -> Iterator[int]:
    directory = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or nofollow is None:
        raise OSError("platform cannot safely open artifact directories")
    flags = os.O_RDONLY | directory | nofollow
    if parent_fd is None:
        if path is None:
            raise ValueError("artifact directory path is required")
        fd = os.open(path, flags)
    else:
        if name is None:
            raise ValueError("artifact directory name is required")
        fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError("artifact root must be a real directory")
        yield fd
    finally:
        os.close(fd)


def _require_root_entries(root_fd: int, expected: dict[str, str]) -> None:
    if set(os.listdir(root_fd)) != set(expected):
        raise ValueError("artifact root entries are not canonical")
    for name, kind in expected.items():
        mode = os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode
        valid = stat.S_ISREG(mode) if kind == "file" else stat.S_ISDIR(mode)
        if not valid:
            raise ValueError(f"{name} is not a regular {kind}")


def _read_regular(root_fd: int, name: str) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform cannot safely open artifact files")
    fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=root_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{name} is not a regular file")
        if metadata.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"{name} exceeds the byte limit")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            content = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"{name} exceeds the byte limit")
        return content
    finally:
        os.close(fd)


def _parse_json[ModelT: BaseModel](content: bytes, model: type[ModelT], name: str) -> ModelT:
    try:
        return model.model_validate_json(content)
    except (ValidationError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {name}: {error}") from error


def _meaningful_patch(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    changed = [line[4:] for line in text.splitlines() if line.startswith(("+++ ", "--- "))]
    return bool(changed) and any(not path.endswith((".pyc", ".pyo")) for path in changed)


def _rebuild_ledger(chain: StrictChainExport, directory: Path) -> Ledger:
    ledger = Ledger(directory / "ledger.db")
    with ledger._connect() as connection:  # noqa: SLF001 - verifier owns this temporary ledger
        for raw in chain.blocks:
            block = Block.model_validate(raw.model_dump())
            connection.execute(
                "INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    block.run_id,
                    block.seq,
                    str(block.block_type),
                    json.dumps(block.payload),
                    block.git_hash,
                    block.prev_hash,
                    block.content_hash,
                    block.created_at.isoformat(),
                    block.schema_version,
                ),
            )
        connection.execute(
            "INSERT INTO chain_heads VALUES (?, ?, ?, ?)",
            (chain.run_id, chain.head.length, chain.head.head_hash, "offline-verifier"),
        )
    return ledger


def _verify_bundle(root_fd: int) -> tuple[StrictManifest, RunReport]:
    _require_root_entries(root_fd, dict.fromkeys(ARTIFACT_FILENAMES, "file"))
    contents = {name: _read_regular(root_fd, name) for name in ARTIFACT_FILENAMES}
    manifest = _parse_json(contents["manifest.json"], StrictManifest, "manifest.json")
    report_data = _parse_json(contents["report.json"], StrictRunReport, "report.json")
    report = RunReport.model_validate(report_data.model_dump())
    chain = _parse_json(contents["chain.json"], StrictChainExport, "chain.json")
    if manifest.run_id != report.run_id or chain.run_id != report.run_id:
        raise ValueError("run IDs disagree")
    if manifest.expected_outcome != str(report.outcome):
        raise ValueError("manifest outcome disagrees with report")
    if set(manifest.artifacts) != set(ARTIFACT_FILENAMES[1:]):
        raise ValueError("manifest artifact set is not canonical")
    for name in ARTIFACT_FILENAMES[1:]:
        digest = manifest.artifacts[name]
        content = contents[name]
        if digest.bytes != len(content) or digest.sha256 != hashlib.sha256(content).hexdigest():
            raise ValueError(f"digest mismatch for {name}")
    if report.outcome is Outcome.SUCCESS:
        if report.branch is None or report.acceptance is None or not report.acceptance.passed:
            raise ValueError("success report lacks a retained branch or passed acceptance")
        if not _meaningful_patch(contents["diff.patch"]):
            raise ValueError("success report lacks a meaningful source patch")
    if report.outcome is Outcome.REFUSED and (report.branch is not None or not report.evidence):
        raise ValueError("refused report must have no branch and a reason")
    with tempfile.TemporaryDirectory(prefix="autodev_artifact_verify_") as temporary:
        ledger = _rebuild_ledger(chain, Path(temporary))
        status = ledger.verify_chain(report.run_id)
        if not status.valid:
            raise ValueError(f"chain integrity failed: {status.reason}")
        provenance = ledger.provenance(report.run_id)
        if not provenance.chain or not provenance.chain.valid:
            raise ValueError("chain provenance is invalid")
        blocks = ledger.blocks(report.run_id)
    if not blocks or blocks[-1].block_type is not BlockType.RUN_END:
        raise ValueError("chain does not end with RUN_END")
    if blocks[-1].payload.get("outcome") != str(report.outcome):
        raise ValueError("RUN_END outcome disagrees with report")
    return manifest, report


def verify(root: Path) -> tuple[bool, str]:
    try:
        with _open_directory(root) as root_fd:
            _verify_bundle(root_fd)
    except (OSError, ValueError, ValidationError) as error:
        return False, str(error)
    return (
        True,
        "bundle is internally consistent; origin and post-export integrity are unauthenticated",
    )


def _verify_child(parent_fd: int, name: str) -> tuple[StrictManifest, RunReport]:
    with _open_directory(parent_fd=parent_fd, name=name) as child_fd:
        return _verify_bundle(child_fd)


def verify_self_improvement(root: Path) -> tuple[bool, str]:
    try:
        with _open_directory(root) as root_fd:
            _require_root_entries(
                root_fd, {"contrast.json": "file", "control": "directory", "primed": "directory"}
            )
            contrast = _parse_json(
                _read_regular(root_fd, "contrast.json"), SelfImprovementContrast, "contrast.json"
            )
            try:
                control_manifest, control_report = _verify_child(root_fd, "control")
            except (OSError, ValueError, ValidationError) as error:
                raise ValueError(f"invalid control bundle: {error}") from error
            try:
                primed_manifest, primed_report = _verify_child(root_fd, "primed")
            except (OSError, ValueError, ValidationError) as error:
                raise ValueError(f"invalid primed bundle: {error}") from error
        control_ids = {control_manifest.run_id, control_report.run_id}
        if contrast.runs.control not in control_ids or len(control_ids) != 1:
            raise ValueError("control run ID does not bind to its bundle")
        primed_ids = {primed_manifest.run_id, primed_report.run_id}
        if contrast.runs.primed not in primed_ids or len(primed_ids) != 1:
            raise ValueError("primed run ID does not bind to its bundle")
        if contrast.runs.control == contrast.runs.primed:
            raise ValueError("control and primed run IDs must differ")
        for label, report in (("control", control_report), ("primed", primed_report)):
            passed_acceptance = report.acceptance is not None and report.acceptance.passed
            if report.outcome is not Outcome.SUCCESS or not passed_acceptance:
                raise ValueError(f"{label} report is not a successful passed-acceptance run")
        if not contrast.acceptance.control or not contrast.acceptance.primed:
            raise ValueError("contrast reports failed acceptance")
        if contrast.check.control or not contrast.check.primed:
            raise ValueError("contrast does not show control false and primed true")
    except (OSError, ValueError, ValidationError) as error:
        return False, str(error)
    return (
        True,
        "self-improvement contrast is internally consistent; "
        "origin and post-export integrity are unauthenticated",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify offline autodev evidence artifacts.")
    parser.add_argument("artifact_dir", type=Path, nargs="?")
    parser.add_argument("--self-improvement", type=Path, dest="self_improvement_dir")
    args = parser.parse_args(argv)
    if (args.artifact_dir is None) == (args.self_improvement_dir is None):
        parser.error("provide either artifact_dir or --self-improvement DIR")
    if args.self_improvement_dir is not None:
        valid, message = verify_self_improvement(args.self_improvement_dir)
    else:
        valid, message = verify(args.artifact_dir)
    print(message)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
