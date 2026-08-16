"""Offline tests for the canonical demo evidence bundle contract."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from artifacts import (  # noqa: E402
    ContrastAcceptance,
    ContrastCheck,
    ContrastRuns,
    SelfImprovementContrast,
    safe_output_name,
    write_run_bundle,
    write_self_improvement_contrast,
)
from verify_demo_artifacts import main, verify, verify_self_improvement  # noqa: E402

from self_improving_coding_agent.contracts import (  # noqa: E402
    AcceptanceResult,
    BlockType,
    Outcome,
    RunReport,
    Ticket,
)
from self_improving_coding_agent.ledger import Ledger  # noqa: E402

SOURCE_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old = 1
+new = 2
"""
BYTECODE_ONLY_PATCH = (
    "diff --git a/__pycache__/app.cpython-312.pyc "
    "b/__pycache__/app.cpython-312.pyc\n"
    "--- a/__pycache__/app.cpython-312.pyc\n"
    "+++ b/__pycache__/app.cpython-312.pyc\n"
)


def _report(
    outcome: Outcome,
    *,
    run_id: str | None = None,
    branch: str | None = None,
    acceptance_exit: int | None = None,
    evidence: str = "refused: unsafe request",
) -> RunReport:
    return RunReport(
        run_id=run_id or f"artifact-{outcome}",
        ticket=Ticket(id="ART-1", repository="/offline-fixture", request="fixture request"),
        branch=branch,
        outcome=outcome,
        acceptance=(
            AcceptanceResult(command="pytest", exit_code=acceptance_exit)
            if acceptance_exit is not None
            else None
        ),
        evidence=evidence,
    )


def _bundle(tmp_path: Path, report: RunReport, *, dest: str = "bundle") -> Path:
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.append_block(report.run_id, BlockType.RUN_START, {"ticket_id": report.ticket.id})
    if report.acceptance is not None:
        ledger.append_block(
            report.run_id,
            BlockType.ACCEPTANCE_GATE,
            {"exit_code": report.acceptance.exit_code, "passed": report.acceptance.passed},
        )
    ledger.append_block(report.run_id, BlockType.RUN_END, {"outcome": str(report.outcome)})
    return write_run_bundle(
        report=report, ledger=ledger, dest=tmp_path / dest, trace="offline fixture\n"
    )


def _self_improvement_bundle(
    tmp_path: Path,
    *,
    control_check: bool = False,
    contrast_control_run_id: str | None = None,
) -> Path:
    root = tmp_path / "self-improvement"
    control = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            run_id="control-run",
            branch="autodev/control",
            acceptance_exit=0,
            evidence=SOURCE_PATCH,
        ),
        dest="self-improvement/control",
    )
    primed = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            run_id="primed-run",
            branch="autodev/primed",
            acceptance_exit=0,
            evidence=SOURCE_PATCH,
        ),
        dest="self-improvement/primed",
    )
    assert control == root / "control"
    assert primed == root / "primed"
    write_self_improvement_contrast(
        contrast=SelfImprovementContrast(
            scenario="offline-fixture",
            primed_rule="Use the stored lesson.",
            runs=ContrastRuns(
                control=contrast_control_run_id or "control-run", primed="primed-run"
            ),
            acceptance=ContrastAcceptance(control=True, primed=True),
            check=ContrastCheck(label="required check", control=control_check, primed=True),
        ),
        dest=root,
    )
    return root


def test_verifier_accepts_a_complete_success_bundle(tmp_path):
    bundle = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            branch="autodev/artifact-success",
            acceptance_exit=0,
            evidence=SOURCE_PATCH,
        ),
    )

    assert verify(bundle) == (
        True,
        "bundle is internally consistent; origin and post-export integrity are unauthenticated",
    )


def test_verifier_accepts_a_complete_refusal_bundle(tmp_path):
    bundle = _bundle(tmp_path, _report(Outcome.REFUSED))

    assert verify(bundle)[0]


def test_verifier_rejects_a_tampered_artifact_digest(tmp_path):
    bundle = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            branch="autodev/artifact-success",
            acceptance_exit=0,
            evidence=SOURCE_PATCH,
        ),
    )
    (bundle / "diff.patch").write_text(SOURCE_PATCH + "+unrecorded change\n")

    valid, message = verify(bundle)

    assert not valid
    assert "digest mismatch for diff.patch" in message


def test_verifier_rejects_success_with_only_bytecode_patch(tmp_path):
    bundle = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            branch="autodev/artifact-success",
            acceptance_exit=0,
            evidence=BYTECODE_ONLY_PATCH,
        ),
    )

    valid, message = verify(bundle)

    assert not valid
    assert "meaningful source patch" in message


@pytest.mark.parametrize(
    ("branch", "acceptance_exit"),
    [(None, 0), ("autodev/artifact-success", 1)],
    ids=["missing-branch", "red-acceptance"],
)
def test_verifier_rejects_semantically_invalid_success(
    tmp_path, branch: str | None, acceptance_exit: int
):
    bundle = _bundle(
        tmp_path,
        _report(
            Outcome.SUCCESS,
            branch=branch,
            acceptance_exit=acceptance_exit,
            evidence=SOURCE_PATCH,
        ),
    )

    valid, message = verify(bundle)

    assert not valid
    assert "lacks a retained branch or passed acceptance" in message


def test_self_improvement_verifier_accepts_valid_contrast_and_cli(tmp_path, capsys):
    bundle = _self_improvement_bundle(tmp_path)

    assert verify_self_improvement(bundle) == (
        True,
        "self-improvement contrast is internally consistent; "
        "origin and post-export integrity are unauthenticated",
    )
    assert main(["--self-improvement", str(bundle)]) == 0
    assert "self-improvement contrast is internally consistent" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("control_check", "contrast_control_run_id", "message"),
    [
        (True, None, "contrast does not show control false and primed true"),
        (False, "unbound-control-run", "control run ID does not bind to its bundle"),
    ],
    ids=["control-already-passes", "control-run-id-mismatch"],
)
def test_self_improvement_verifier_rejects_invalid_contrast(
    tmp_path, control_check: bool, contrast_control_run_id: str | None, message: str
):
    bundle = _self_improvement_bundle(
        tmp_path,
        control_check=control_check,
        contrast_control_run_id=contrast_control_run_id,
    )

    valid, result = verify_self_improvement(bundle)

    assert not valid
    assert message in result


def test_self_improvement_verifier_rejects_run_bundle_with_unexpected_file(tmp_path):
    bundle = _self_improvement_bundle(tmp_path)
    (bundle / "primed" / "unexpected.txt").write_text("offline tampering\n")

    valid, message = verify_self_improvement(bundle)

    assert not valid
    assert "invalid primed bundle: artifact root entries are not canonical" in message


def test_artifact_output_name_rejects_escape_paths_before_writing(tmp_path):
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "escape"

    for unsafe_name in ("../escape", str(outside)):
        with pytest.raises(ValueError, match="unsafe output name"):
            destination = base / safe_output_name(unsafe_name)
            _bundle(tmp_path, _report(Outcome.REFUSED), dest=str(destination))

    assert not outside.exists()
    assert list(base.iterdir()) == []


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")


def test_verifier_rejects_a_symlinked_artifact_root(tmp_path):
    bundle = _bundle(tmp_path, _report(Outcome.REFUSED))
    alias = tmp_path / "bundle-alias"
    _symlink_or_skip(alias, bundle, directory=True)

    valid, message = verify(alias)

    assert not valid
    assert message


def test_verifier_rejects_a_symlinked_artifact_file(tmp_path):
    bundle = _bundle(tmp_path, _report(Outcome.REFUSED))
    replacement = tmp_path / "untrusted-trace.log"
    replacement.write_text("untrusted content\n")
    (bundle / "trace.log").unlink()
    _symlink_or_skip(bundle / "trace.log", replacement)

    valid, message = verify(bundle)

    assert not valid
    assert "trace.log is not a regular file" in message
