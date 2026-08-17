"""App2's memory-only audit check must inspect, never execute, agent-authored code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo import TARGET_APP_2, materialize  # noqa: E402
from demo_selfimprove import SCENARIOS  # noqa: E402


def _commit(repo: Path, path: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", path], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "test fixture"], check=True)


def test_app2_seed_does_not_already_satisfy_the_recalled_audit_policy(tmp_path):
    repo = materialize(tmp_path / "repo", TARGET_APP_2)

    assert SCENARIOS["app2"].check(repo, "HEAD") is False


def test_app2_audit_check_recognizes_the_policy_without_executing_source(tmp_path, monkeypatch):
    repo = materialize(tmp_path / "repo", TARGET_APP_2)
    sentinel = tmp_path / "executed"
    monkeypatch.setenv("AUTODEV_TEST_SENTINEL", str(sentinel))
    (repo / "service.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['AUTODEV_TEST_SENTINEL']).write_text('executed')\n"
        "class OrderService:\n"
        "    def _authorize_read(self, token, order_id):\n"
        "        try:\n"
        "            require_owner(None, None)\n"
        "            return order_id\n"
        "        except ForbiddenError:\n"
        "            self._audit.record(AuditEvent())\n"
        "    def get_order(self, token, order_id):\n"
        "        return self._authorize_read(token, order_id)\n"
        "    def order_summary(self, token, order_id):\n"
        "        return self._authorize_read(token, order_id)\n"
    )
    _commit(repo, "service.py")

    assert SCENARIOS["app2"].check(repo, "HEAD") is True
    assert not sentinel.exists()
