"""Per-run git worktree — the isolation jail and trust boundary.

Every change happens on a dedicated branch inside a dedicated worktree, never on the
target's checked-out branch. Paths are confined to the worktree root, commands run
without a shell, git hooks/fsmonitor from the target repo are disabled, and child
processes get a scrubbed environment so they can't read our credentials. This is where
untrusted ticket input is contained, in code.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .acceptance_policy import POLICIES, AcceptanceRejected, normalize, resolve

BRANCH_PREFIX = "autodev/"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Disable anything in a target repo that turns a plain git command into code execution.
_GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
    "-c", "protocol.ext.allow=never",
]

# Which runners, flags and paths an acceptance command may use lives in acceptance_policy
# as data. ACCEPTANCE_ALLOWLIST stays as the set of permitted runners for callers that only
# need the names.
ACCEPTANCE_ALLOWLIST = frozenset(POLICIES)


class WorktreeError(RuntimeError):
    pass


class WorktreeEscape(WorktreeError):
    """Raised when a path or operation tries to leave the worktree."""


_ISOLATED_HOME: str | None = None


def _isolated_home() -> str:
    # An empty throwaway HOME so child git commands can't reach ~/.gitconfig. Shared across
    # git calls, which carry no untrusted code; Worktree.run passes its own per-run HOME.
    global _ISOLATED_HOME
    if _ISOLATED_HOME is None:
        _ISOLATED_HOME = tempfile.mkdtemp(prefix="autodev_home_")
    return _ISOLATED_HOME


def _safe_path_entries() -> str:
    """PATH with relative entries dropped.

    Children run with cwd inside the worktree, so a relative (or empty) PATH component would
    let the target repo's own `./pytest` be the thing we execute.
    """
    entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(p)]
    return os.pathsep.join(entries)


def _safe_env(home: str | None = None) -> dict[str, str]:
    """Minimal environment for child processes. No AWS/Bedrock env creds, and HOME is
    redirected to an empty dir so the target's own tests can't read file/profile-based
    credentials (~/.aws, ~/.ssh) — env-stripping alone wouldn't stop that."""
    keep = ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "SystemRoot")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = _safe_path_entries()
    home = home or _isolated_home()
    env["HOME"] = home
    # Don't import anything from a user site-packages dir: gate code could otherwise plant a
    # usercustomize.py under HOME that a later run would import automatically.
    env["PYTHONNOUSERSITE"] = "1"
    # No stray __pycache__ in the worktree, which would dirty the diff and is_clean().
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Deny inherited git + AWS credential/config files.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.path.join(home, "aws-none", "credentials")
    env["AWS_CONFIG_FILE"] = os.path.join(home, "aws-none", "config")
    # Make our own commits attributable without depending on the target's git identity.
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "autodev"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "autodev@localhost"
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the child's whole process group, so a runner that spawned helpers can't
    outlive the budget.

    `start_new_session=True` makes the child's pgid equal its pid, so signal that directly
    rather than asking for a pgid we already know. Escalates to SIGKILL: a failure to signal
    must not skip the escalation, or a survivor keeps the output pipe open and the caller
    blocks. A process that calls setsid() leaves this group and cannot be reached — that
    needs an OS-level sandbox, not a signal.
    """
    if proc.pid == os.getpgrp():  # never signal our own group
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_safe_env(),
    )


class Worktree:
    def __init__(self, repo: Path, root: Path, branch: str):
        self.repo = repo
        self.root = root.resolve()
        self.branch = branch
        # A HOME of this run's own, so gate code can't plant a file under HOME that a later
        # run would import (e.g. usercustomize.py). Removed with the worktree.
        self._home = tempfile.mkdtemp(prefix="autodev_run_home_")

    @classmethod
    def create(cls, repo: Path | str, run_id: str, base_dir: Path) -> Worktree:
        if not _RUN_ID_RE.match(run_id):
            raise WorktreeError(f"invalid run_id: {run_id!r}")
        repo = Path(repo).resolve()
        if not (repo / ".git").exists():
            raise WorktreeError(f"{repo} is not a git repository")
        base_dir = base_dir.resolve()
        root = (base_dir / run_id).resolve()
        if root.parent != base_dir:  # defense in depth against a crafted run_id
            raise WorktreeError(f"run_id escapes base dir: {run_id!r}")
        branch = f"{BRANCH_PREFIX}{run_id}"
        base_dir.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "prune")  # clear any stale record so create is idempotent
        r = _git(repo, "worktree", "add", "-b", branch, str(root), "HEAD")
        if r.returncode != 0:
            raise WorktreeError(f"worktree add failed: {r.stderr.strip()}")
        return cls(repo, root, branch)

    def safe_path(self, path: str | Path) -> Path:
        """Resolve a path and guarantee it stays inside the worktree, or raise.
        Callers must use the returned resolved path, not the original, to avoid TOCTOU."""
        candidate = Path(path)
        base = candidate if candidate.is_absolute() else self.root / candidate
        resolved = base.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorktreeEscape(f"path escapes worktree: {path}")
        return resolved

    def run(self, args: list[str], *, timeout: int = 300) -> CommandResult:
        if not args:
            raise WorktreeError("empty command")
        # Output goes to a temp file, not a pipe: a detached grandchild holding a pipe's
        # write end open would make the post-timeout read block forever, and the wall-clock
        # bound is the point. start_new_session gives the child its own process group so a
        # timeout kills the tree; stdin is closed so nothing can wait on operator input.
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as sink:
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    env=_safe_env(self._home),
                    start_new_session=True,
                )
            except OSError as e:  # runner missing, not executable, ...
                return CommandResult(127, f"could not run {args[0]!r}: {e}")
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
            sink.seek(0)
            output = sink.read().strip()
        if timed_out:
            return CommandResult(124, f"timed out after {timeout}s\n{output}".strip())
        return CommandResult(proc.returncode, output)

    def run_acceptance(self, command: str, *, timeout: int = 300) -> CommandResult:
        """Run a declared acceptance command.

        The command is untrusted ticket text, so it is split (never handed to a shell) and
        then checked token by token against the runner's policy: an unrecognised runner,
        flag, module, or a path resolving outside this worktree is refused. Fail-closed —
        anything the policy doesn't name is denied.
        """
        try:
            args = shlex.split(command)
        except ValueError as e:
            raise WorktreeError(f"unparseable command: {e}") from e
        try:
            argv = resolve(normalize(args), in_jail=self.root)
        except AcceptanceRejected as e:
            raise WorktreeError(f"command not allowed: {command!r} ({e})") from e
        return self.run(argv, timeout=timeout)

    def is_clean(self) -> bool:
        r = _git(self.root, "status", "--porcelain")
        return r.returncode == 0 and r.stdout.strip() == ""

    def diff(self) -> str:
        return _git(self.root, "diff", "HEAD").stdout

    def head_hash(self) -> str | None:
        """The commit this worktree points at. Recorded in ledger blocks so the chain
        references real, restorable state instead of keeping its own copy of it."""
        r = _git(self.root, "rev-parse", "HEAD")
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def commit(self, message: str) -> bool:
        """Commit all changes to the run branch. Returns False if there was nothing to commit."""
        if self.is_clean():
            return False
        _git(self.root, "add", "-A")
        return _git(self.root, "commit", "-m", message).returncode == 0

    def revert(self) -> None:
        """Discard all changes (tracked and untracked) — leaves the working tree clean."""
        _git(self.root, "reset", "--hard", "HEAD")
        _git(self.root, "clean", "-fd")

    def remove(self, *, keep_branch: bool = False) -> None:
        shutil.rmtree(self._home, ignore_errors=True)  # this run's HOME goes with it
        rm = _git(self.repo, "worktree", "remove", "--force", str(self.root))
        if not keep_branch:
            _git(self.repo, "branch", "-D", self.branch)
        _git(self.repo, "worktree", "prune")
        if rm.returncode != 0 and self.root.exists():
            raise WorktreeError(f"worktree remove failed: {rm.stderr.strip()}")
