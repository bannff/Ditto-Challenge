"""The demo is a graded artifact — its materialized repo must produce clean diffs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo import materialize  # noqa: E402
from demo_selfimprove import SCENARIOS  # noqa: E402


def _tracked(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def test_materialize_excludes_bytecode_from_the_seed(tmp_path):
    target = tmp_path / "app"
    (target / "__pycache__").mkdir(parents=True)
    (target / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00binary")
    (target / "mod.py").write_text("x = 1\n")

    repo = materialize(tmp_path / "repo", target)
    tracked = _tracked(repo)

    assert "mod.py" in tracked
    assert not any("pycache" in path or path.endswith(".pyc") for path in tracked)


def test_bytecode_generated_after_seeding_stays_out_of_the_diff(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "mod.py").write_text("x = 1\n")
    repo = materialize(tmp_path / "repo", target)

    # Simulate what running the target's tests does to the tree.
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00binary")

    diff = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout.strip() == ""


def test_ledger_demo_runs_offline_and_shows_every_claim(capsys):
    """The ledger demo must work with no credentials and no network, because that's the
    point of an offline audit — and it has to actually demonstrate each claim, not just
    exit 0."""
    from demo_ledger import main

    assert main([]) == 0
    out = capsys.readouterr().out

    assert "chain:      VERIFIED" in out  # act 1: a clean chain verifies
    assert "CHAIN BROKEN HERE" in out  # act 2: tampering is pinpointed
    assert "chain is truncated" in out  # act 3: a deleted tail is caught
    assert "LEARNS" in out and "REFUSED" in out  # act 4: the gate discriminates
    assert "tamper-EVIDENT, not tamper-proof" in out  # the limits are stated


def test_ledger_demo_saves_a_transcript(tmp_path):
    from demo_ledger import main

    assert main(["--out", str(tmp_path / "bundle")]) == 0
    transcript = tmp_path / "bundle" / "ledger-demo.log"
    assert transcript.exists()
    assert "chain:      VERIFIED" in transcript.read_text()


def test_inventory_self_improvement_check_never_executes_committed_source(tmp_path, monkeypatch):
    target = tmp_path / "app"
    target.mkdir()
    repo = materialize(tmp_path / "repo", target)
    sentinel = tmp_path / "executed"
    monkeypatch.setenv("AUTODEV_TEST_SENTINEL", str(sentinel))
    (repo / "inventory.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['AUTODEV_TEST_SENTINEL']).write_text('executed')\n"
        "class Inventory:\n"
        "    def needs_reorder(self, threshold):\n"
        "        return []\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "inventory.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "malicious inventory fixture"], check=True
    )

    result = SCENARIOS["app1"].check(repo, "HEAD")

    assert result is False
    assert not sentinel.exists()
