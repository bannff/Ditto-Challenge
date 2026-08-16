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
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .acceptance_policy import (
    POLICIES,
    PYTEST_CONFIG_FILES,
    AcceptanceRejected,
    normalize,
    resolve,
)
from .contracts import RUN_ID_RE

BRANCH_PREFIX = "autodev/"

# Checkpoints live outside refs/heads on purpose: not in `git branch`, not a merge target,
# not carried by an ordinary `git push`. A retained *branch* of unverified work is a merge
# accident waiting to happen; a private ref is recoverable state and nothing more.
CHECKPOINT_REF_PREFIX = "refs/autodev/checkpoints/"

# How many runs' checkpoint refs to keep. Retention is effectively unconditional — most runs
# pass at least one node — so it has to be bounded, and the objects behind old refs are
# reclaimed by git's own gc once nothing points at them.
CHECKPOINT_REFS_KEPT = 20

# Ceilings on what one run may commit. Both are bounds on a cost we impose on someone else's
# repository: every checkpoint and every shipped change lands in the *target's* object store
# and stays there, so "bounded runs" has to mean bounded writes, not just bounded time.
# MAX_HIDDEN_FILES additionally caps the blast radius of surfacing files a run's own ignore
# rules hid — an agent that appends `*` to a committed `.gitignore` makes that pattern win for
# every pre-existing ignored path, so naive surfacing would commit a whole `.venv`.
# Policy dials, not security parameters: agent-authored files are bounded by tool calls
# (tens), while a pre-existing ignored tree is thousands, and the gap is what these sit in.
MAX_HIDDEN_FILES = 200
MAX_COMMIT_BYTES = 8 * 1024 * 1024

# One `write_file` call. Large enough for any real source file, small enough that a ticket
# which talks the agent into generating data cannot pack it into the target's history.
MAX_WRITE_BYTES = 256 * 1024

# A run id becomes a path component and a ref name, so it is validated before it reaches
# either. Canonical in contracts because the ledger and cassette paths need the same
# guarantee — two copies of a safety regex is a drift bug waiting to happen.

# A commit hash read back from a ledger payload is untrusted text before it reaches git: a
# value starting with `-` would be parsed as a flag.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")

# Disable anything in a target repo that turns a plain git command into code execution or
# makes our own evidence lie. `-c` cannot wildcard a subsection, so `filter.<name>.clean` and
# `diff.<name>.textconv` — both run through /bin/sh — are unreachable from here. They are
# closed by denying writes to the config instead: `.git` is refused at the tool layer
# (_DENIED_COMPONENTS) and every in-worktree git call pins --git-dir/--work-tree, so a
# clobbered gitdir pointer is inert and core.worktree cannot re-aim `clean -fd`.
_GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
    "-c", "protocol.ext.allow=never",
    "-c", "core.excludesFile=/dev/null",
    "-c", "core.attributesFile=/dev/null",
]

# Path components the file tools never touch, whatever the containment check says. `.git` is
# the repo's control plane, not source: in a linked worktree it is a *file* (the gitdir
# pointer), so one in-jail write redirects every later git command at a repo whose config the
# agent wrote — and git config executes filter/textconv commands through /bin/sh.
# `.gitattributes` is here for the same reason: it is git configuration that ships *inside*
# the tree, so no `-c` flag can disable it. One line marking a path `-diff` turns a real
# change into "Binary files differ" in the evidence a reviewer reads, and a `filter=` or
# `diff=<driver>` entry runs a command through /bin/sh for anyone who later runs git in that
# tree with a matching driver in their own config. Denied at any depth, since git reads it
# per-directory.
_DENIED_COMPONENTS = frozenset({".git", ".gitattributes"})


def _fold_component(component: str) -> str:
    """Normalise one path component to the form the filesystem will match on.

    `Path.resolve()` preserves the spelling as typed, but APFS and NTFS are
    case-insensitive, so `.GIT/config` opens `.git/config` (verified). Unicode format
    characters are dropped and trailing dots/spaces stripped because HFS+ ignores the former
    and Windows strips the latter — both make `.git` reachable under another name.
    """
    stripped = "".join(c for c in component if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFC", stripped).rstrip(". ").casefold()


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


def _git(
    repo: Path,
    *args: str,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    timeout: int = 60,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git with the hardening flags, optionally pinned to a known gitdir/work tree.

    Pinning is how a repository stops being discovered from inside the jail: with
    --git-dir given, `<root>/.git` is never read, and --work-tree on the command line
    outranks any `core.worktree` in config. Paths are absolute so `-C` cannot re-root them.

    `stdin` feeds commands that take a path list that way (`check-ignore --stdin`). Passing
    thousands of paths as argv would hit ARG_MAX on a repo with a large ignored tree.
    """
    pinned = [f"--git-dir={git_dir}"] if git_dir else []
    if work_tree:
        pinned.append(f"--work-tree={work_tree}")
    return subprocess.run(
        ["git", *_GIT_HARDENING, *pinned, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",  # repo bytes are not guaranteed UTF-8; a decode error must not
        timeout=timeout,   # escape a caller that only handles WorktreeError
        check=False,
        env=_safe_env(),
        input=stdin,
    )


def is_descendant(repo: Path, ancestor: str, commit: str) -> bool:
    """True when `commit` is `ancestor` or descends from it. Both must look like hashes."""
    if not (_COMMIT_RE.match(ancestor) and _COMMIT_RE.match(commit)):
        return False
    if ancestor == commit:
        return True
    return (
        _git(repo, "merge-base", "--is-ancestor", ancestor, commit, "--").returncode == 0
    )


def resolve_checkpoint(repo: Path, run_id: str) -> str | None:
    """The commit a run's checkpoint ref names, or None.

    Module-level rather than a `Worktree` method because recovery happens when there is no
    worktree and no seed left — the run is over. This is also the *authority* for which
    commit gets recovered: git's ref store is written by `checkpoint()` and never by the
    ledger, so a hash read out of an unsigned SQLite row corroborates it but never chooses it.
    """
    if not RUN_ID_RE.match(run_id):
        return None
    ref = f"{CHECKPOINT_REF_PREFIX}{run_id}"
    if not ref.startswith(CHECKPOINT_REF_PREFIX):  # belt: a looser regex later can't escape
        return None
    r = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    commit = r.stdout.strip()
    if r.returncode != 0 or not _COMMIT_RE.match(commit):
        return None
    return commit


class Worktree:
    def __init__(self, repo: Path, root: Path, branch: str):
        self.repo = repo
        self.root = root.resolve()
        self.branch = branch
        self.run_id = branch.removeprefix(BRANCH_PREFIX)
        # The real gitdir, read once before the agent has touched anything, and used on every
        # later call instead of re-discovering it through `<root>/.git`. For a linked worktree
        # it lives under the target repo, outside the jail, so no file tool can reach it.
        self._git_dir = self._discover_git_dir()
        # The commit this run starts from, read before the agent can touch anything. Every
        # revert returns here, so it must not be re-read later: once a checkpoint commit
        # lands, HEAD has moved and "back to the start" would mean the wrong thing.
        self._seed = self._read_seed()
        # A HOME of this run's own, so gate code can't plant a file under HOME that a later
        # run would import (e.g. usercustomize.py). Removed with the worktree.
        self._home = tempfile.mkdtemp(prefix="autodev_run_home_")
        # Why the last `commit()` declined, so a caller can report it instead of guessing.
        self.last_commit_refusal: str | None = None

    def _read_seed(self) -> str:
        r = self._wt_git("rev-parse", "HEAD")
        seed = r.stdout.strip()
        if r.returncode != 0 or not _COMMIT_RE.match(seed):
            raise WorktreeError(f"cannot resolve the starting commit for {self.root}")
        return seed

    def _discover_git_dir(self) -> Path:
        r = _git(self.root, "rev-parse", "--absolute-git-dir")
        gitdir = r.stdout.strip()
        if r.returncode != 0 or not gitdir:
            raise WorktreeError(f"cannot resolve the gitdir for {self.root}: {r.stderr.strip()}")
        return Path(gitdir)

    def _wt_git(
        self, *args: str, timeout: int = 60, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _git(
            self.root,
            *args,
            git_dir=self._git_dir,
            work_tree=self.root,
            timeout=timeout,
            stdin=stdin,
        )

    @classmethod
    def prune_checkpoints(cls, repo: Path, keep: int = CHECKPOINT_REFS_KEPT) -> int:
        """Drop all but the `keep` most recent checkpoint refs. Returns how many were removed.

        Only ever touches `refs/autodev/checkpoints/*` — never `refs/heads/*`.
        """
        listed = _git(
            repo,
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname)",
            f"{CHECKPOINT_REF_PREFIX}*",
        )
        if listed.returncode != 0:
            return 0
        refs = [
            line
            for line in listed.stdout.splitlines()
            if line.startswith(CHECKPOINT_REF_PREFIX)
        ]
        removed = 0
        for ref in refs[keep:]:
            if _git(repo, "update-ref", "-d", ref).returncode == 0:
                removed += 1
        return removed

    @classmethod
    def create(cls, repo: Path | str, run_id: str, base_dir: Path) -> Worktree:
        if not RUN_ID_RE.match(run_id):
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
        for part in resolved.relative_to(self.root).parts:
            if _fold_component(part) in _DENIED_COMPONENTS:
                raise WorktreeEscape(f"path is git metadata, not source: {path}")
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
            argv = resolve(
                normalize(args),
                in_jail=self.root,
                config=self._gate_config(),
                home=Path(self._home),
            )
        except AcceptanceRejected as e:
            raise WorktreeError(f"command not allowed: {command!r} ({e})") from e
        return self.run(argv, timeout=timeout)

    def _gate_config(self) -> Path:
        """The config file the gate runs with — pinned, and pinned to *committed* content.

        Two jobs. Pinning `-c` at all stops pytest walking up past the worktree and adopting
        a directory above the jail as its config. Pinning it to the config that shipped with
        the target at this run's base commit stops the agent *writing* the gate's own config:
        an ini it authors is a write primitive (`log_file` creates and appends outside the
        jail), an import path (`pythonpath` puts any directory on `sys.path`) and a verdict
        switch (`python_files = test_green.py` drops the failing file, so a red suite exits
        0). Ignoring agent-authored config closes all of those at once, including the keys
        nobody has enumerated yet — a forced `-o` per dangerous key would not.

        Anchored to `self._seed`, never to HEAD: `checkpoint()` commits the agent's own work,
        so after one checkpoint `HEAD:pytest.ini` *is* the agent's file.

        Three outcomes, in order of fidelity:
          - shipped and untouched -> the target's own file, used in place, so relative
            `paths` keys (`pythonpath = src`) still resolve against the worktree;
          - shipped but modified (or deleted) -> the committed blob, copied into this run's
            HOME, so the target keeps its markers/filterwarnings/asyncio_mode and only the
            agent's edit is dropped;
          - not shipped -> an empty ini of ours.
        """
        for name, marker in PYTEST_CONFIG_FILES:
            shipped = self._wt_git("cat-file", "blob", f"{self._seed}:{name}")
            if shipped.returncode != 0:  # not in the target at the base commit
                continue
            # The marker is checked against the committed text, so an empty pyproject.toml
            # can't be given a pytest section by the agent to shadow a real tox.ini.
            if marker is not None and marker not in shipped.stdout:
                continue
            untouched = self._wt_git("diff", "--quiet", self._seed, "--", name)
            if untouched.returncode == 0 and (self.root / name).is_file():
                return self.root / name
            return self._own_config(name, shipped.stdout)
        return self._own_config("gate-pytest.ini", "[pytest]\n")

    def _own_config(self, name: str, body: str) -> Path:
        """Write a gate config into this run's HOME, which is outside the jail.

        Rewritten on every call rather than reused: gate code runs as our uid, so a previous
        gate run could have edited this file, and a stale copy would be config the agent
        reached after all. The name is preserved because pytest picks its parser by suffix —
        `pyproject.toml` is only TOML if it is still called that.
        """
        path = Path(self._home) / name
        path.write_text(body)
        return path

    def _seed_patterns(self, source: str) -> frozenset[str]:
        """The ignore rules `source` shipped with at this run's base commit.

        An absolute path or one climbing out of the tree is not in-tree content — that is
        `<repo>/.git/info/exclude`, which only gate code can write — so it has no seed blob
        and every rule in it is untrusted. Same answer for a file this run created.
        """
        path = Path(source)
        if path.is_absolute() or ".." in path.parts:
            return frozenset()
        blob = self._wt_git("cat-file", "blob", f"{self._seed}:{source}")
        if blob.returncode != 0:
            return frozenset()
        return frozenset(
            line.strip()
            for line in blob.stdout.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    def _ignored_by_this_run(self) -> list[str]:
        """Paths that only *this run's own* ignore rules keep out of the evidence.

        `.gitignore` is legitimately writable — adding a build artifact to it is real work —
        but git's ignore rules apply to our own bookkeeping, so an agent-authored rule edits
        what we can see. `write_file(".gitignore", "stash.py")` plus `write_file("stash.py")`
        made `is_clean()` return True, kept the file out of `diff()`, and kept `add -A` from
        committing it: the branch we shipped omitted a file the passing test imported, and the
        ticket's own acceptance command then exited non-zero on that branch while the run
        reported SUCCESS. The deliverable didn't reproduce the verdict.

        Trust is per *pattern*, not per file, and anchored to `self._seed` like `_gate_config`:
        a rule stays in force if the line that matched came with the target. That keeps a
        legitimate append (adding `dist/` to a committed `.gitignore`) from surfacing the
        `.venv/` the same file already ignored, which is what makes this safe to switch on —
        for an ordinary target the result is empty and every path below is unchanged.

        Surfacing rather than refusing, because surfacing restores exactly the behaviour the
        target had before the edit: a path no seed rule hides would already have been in the
        diff and the commit. Fail-closed in both directions — a `status` we can't read, or a
        candidate git won't explain, counts as hidden.
        """
        listed = self._wt_git("status", "--porcelain", "-z", "--ignored=matching")
        if listed.returncode != 0:
            return ["<status unavailable>"]
        # `matching`, not the default `traditional`: traditional collapses an untracked
        # directory of ignored files into one `!! sub/` entry, which check-ignore then reports
        # as *not* ignored, and we would have to re-implement the recursion to find out why.
        candidates = [e[3:] for e in listed.stdout.split("\0") if e.startswith("!! ")]
        if not candidates:
            return []
        verdicts = self._wt_git(
            "check-ignore", "-v", "-z", "--no-index", "--stdin",
            stdin="\0".join(candidates) + "\0",
        )
        fields = verdicts.stdout.split("\0")
        # -z emits four NUL-separated fields per record: source, line number, pattern, path.
        records = [fields[i : i + 4] for i in range(0, len(fields) - 3, 4)]
        surfaced = {path for path in candidates if path not in {r[3] for r in records}}
        cache: dict[str, frozenset[str]] = {}
        for source, _line, pattern, path in records:
            if pattern not in cache.setdefault(source, self._seed_patterns(source)):
                surfaced.add(path)
        return sorted(surfaced)

    def _tree_cost(self, extra: list[str]) -> tuple[int, int]:
        """(files, bytes) this run would add, counting `extra` paths git is ignoring.

        Directories are expanded here rather than by git because the paths in `extra` are
        ignored, so no git command will enumerate them for us. Stops early once both ceilings
        are exceeded: the point is to answer "too big?", not to measure precisely.
        """
        files = size = 0
        pending = [
            entry[3:]
            for entry in self._wt_git("status", "--porcelain", "-z").stdout.split("\0")
            if len(entry) > 3 and not entry.startswith("D ")
        ]
        for rel in dict.fromkeys([*pending, *extra]):
            target = self.root / rel
            found = target.rglob("*") if target.is_dir() else [target]
            for item in found:
                if not item.is_file() or item.is_symlink():
                    continue
                files += 1
                with contextlib.suppress(OSError):
                    size += item.stat().st_size
                if files > MAX_HIDDEN_FILES and size > MAX_COMMIT_BYTES:
                    return files, size
        return files, size

    def _why_not_committable(self, surfaced: list[str]) -> str | None:
        """Why this tree must not be committed as it stands, if so.

        Both ceilings exist because the alternative to refusing is worse than refusing. If an
        agent appends `*` to a committed `.gitignore`, that pattern wins for every
        pre-existing ignored path too, so surfacing would commit a whole `.venv` — measured at
        1500 files and a 315KB diff. And every commit here lands in the *target's* object
        store permanently, so unbounded writes are a durable cost to a repo we don't own.
        Refusing turns both into a stated failure that reverts, which is the outcome the
        product asks for.
        """
        files, size = self._tree_cost(surfaced)
        if len(surfaced) > MAX_HIDDEN_FILES or files > MAX_HIDDEN_FILES:
            return (
                f"this run's own ignore rules hide {len(surfaced)} path(s) covering "
                f"{files} files, over the {MAX_HIDDEN_FILES} allowed"
            )
        if size > MAX_COMMIT_BYTES:
            return f"the change is {size} bytes, over the {MAX_COMMIT_BYTES} allowed"
        return None

    def is_clean(self) -> bool:
        r = self._wt_git("status", "--porcelain")
        if r.returncode != 0 or r.stdout.strip() != "":
            return False
        # A file hidden by a rule this run wrote is still a change this run made. Without
        # this, an agent that ignores its own output leaves a "clean" tree that isn't.
        return not self._ignored_by_this_run()

    def diff(self) -> str:
        """The change this run made, as the reviewer and the ledger see it.

        Four things a plain `git diff HEAD` gets wrong here, all of which silently shrink the
        evidence rather than erroring:

        - Untracked files are invisible to it, so a ticket that adds a new module produced
          an *empty* diff. `add -N` records intent-to-add so new files show up as additions
          without staging their contents.
        - A `.gitattributes` marking a path `-diff` (or pointing it at a textconv driver)
          reduces a real change to "Binary files differ". `--text --no-textconv` forces the
          content, so an in-tree attribute can't decide what a reviewer is allowed to read.
        - `add -N -- .` skips paths git is ignoring, so a rule this run wrote removes a file
          from the evidence. `add -Nf` on exactly those paths puts them back.
        - HEAD is the wrong base once anything has been checkpointed: `checkpoint()` commits
          the agent's work, so after the first passing node `diff HEAD` describes only the
          *tail* of the run and the report understates what shipped. Anchored to the seed for
          the same reason `revert()` is — "everything this run did" has one meaning.
        """
        self._wt_git("add", "-N", "--", ".")
        surfaced = self._ignored_by_this_run()
        if surfaced and self._why_not_committable(surfaced) is None:
            self._wt_git("add", "-Nf", "--", *surfaced)
        return self._wt_git("diff", "--text", "--no-textconv", self._seed, "--").stdout

    def head_hash(self) -> str | None:
        """The commit this worktree points at. Recorded in ledger blocks so the chain
        references real, restorable state instead of keeping its own copy of it."""
        r = self._wt_git("rev-parse", "HEAD")
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def commit(self, message: str) -> bool:
        """Commit all changes to the run branch, including anything this run tried to hide.

        Returns False when there was nothing to commit *or* when the tree must not ship as it
        stands (see `_why_not_committable`). Callers have to treat False as "nothing shipped"
        rather than ignoring it, or the run reports a branch with no change on it.
        """
        if self.is_clean():
            return False
        surfaced = self._ignored_by_this_run()
        blocked = self._why_not_committable(surfaced)
        if blocked is not None:
            self.last_commit_refusal = blocked
            return False
        # A failed `add` must not fall through to a commit that ships a partial change: an
        # embedded git repo in the tree, for instance, makes `add -A` fatal (verified).
        if self._wt_git("add", "-A").returncode != 0:
            return False
        # `-f` because these are exactly the paths `add -A` just declined to stage.
        if surfaced and self._wt_git("add", "-Af", "--", *surfaced).returncode != 0:
            return False
        return self._wt_git("commit", "-m", message).returncode == 0

    def revert(self) -> None:
        """Discard everything this run did — back to the commit it started from.

        Anchored to the seed, NOT to HEAD. Once a checkpoint commit exists, HEAD is that
        checkpoint, so `reset --hard HEAD` would keep the very change it is supposed to
        throw away: "a change that breaks tests is reverted" would quietly become "reverted
        to the last unverified checkpoint".
        """
        self._wt_git("reset", "--hard", self._seed)
        self._wt_git("clean", "-fdx")
        # And drop the checkpoint ref, or "discard everything this run did" would be true of
        # the working tree and false of the object store: the exact tree the test-gate
        # rejected would stay reachable in the target repo, one command from being restored.
        # Mid-run rollback uses the in-memory checkpoint list, so nothing needs this ref.
        _git(self.repo, "update-ref", "-d", self.checkpoint_ref)

    def has_committed_change(self) -> bool:
        """True when this run's branch carries a commit beyond the one it started from."""
        return self.head_hash() != self._seed

    def finalize(self, message: str) -> bool:
        """Collapse this run's work into one commit and say whether anything shipped.

        A clean tree does not mean nothing happened: once a node checkpoints, the change is
        already committed, so `commit()` alone returns False and a caller reading that as
        "the agent made no change" throws away a verified change. This distinguishes the two.

        The squash is deliberate. Checkpoints are scaffolding for the retry loop, not history
        a reviewer should read, and their messages say the acceptance gate never ran — false
        once it has. One commit against the seed leaves the branch carrying exactly the
        change, under one honest message, which is also the cleanest diff to review.

        Returns False only when the run genuinely produced nothing.
        """
        if self.is_clean() and not self.has_committed_change():
            return False
        if self.has_committed_change():
            # Un-commit the checkpoints, keeping their content staged, so one commit replaces
            # them. Every ceiling in commit() then applies to the whole shipped change.
            self._wt_git("reset", "--soft", self._seed)
        if not self.commit(message):
            return False
        # The change shipped, so there is nothing left to recover: the branch has it, and the
        # checkpoint ref would otherwise dangle at a superseded tree. Recovery is for work
        # that did NOT ship.
        _git(self.repo, "update-ref", "-d", self.checkpoint_ref)
        return True

    def checkpoint(self, node: str) -> str | None:
        """Commit the current tree as a recoverable checkpoint, returning its hash.

        These commits pass a node's *eval checkpoint* — LLM judges plus swarm status — not
        the acceptance gate, so the message says so. They exist so a failed attempt can be
        rolled back to a known tree instead of the next attempt inheriting half-applied
        edits, and so a ledger block can reference real restorable state.

        Kept on `refs/autodev/checkpoints/<run_id>`, deliberately not a branch: it never
        appears in `git branch`, is not a merge target, and is not carried by an ordinary
        `git push`. A retained *branch* of unverified work is a merge accident waiting to
        happen.
        """
        if not self.commit(f"autodev: checkpoint {node} (eval-passed; acceptance gate not run)"):
            return None
        commit = self.head_hash()
        if commit is None:
            return None
        # The ref is what keeps these commits reachable after the run branch is deleted.
        if _git(self.repo, "update-ref", self.checkpoint_ref, commit).returncode != 0:
            return None
        return commit

    def restore(self, commit: str) -> bool:
        """Reset the tree to a commit this run made. Returns False if it is not ours.

        The hash arrives from a ledger payload, i.e. a file someone may have edited, so it
        is shape-checked before it reaches git (a value starting with `-` would otherwise be
        read as a flag) and then checked for ancestry — restoring an arbitrary commit is not
        recovery.
        """
        if not _COMMIT_RE.match(commit):
            return False
        if not self._is_ours(commit):
            return False
        if self._wt_git("reset", "--hard", commit).returncode != 0:
            return False
        self._wt_git("clean", "-fdx")
        return True

    def _is_ours(self, commit: str) -> bool:
        """True when the commit is this run's seed or descends from it."""
        return is_descendant(self.repo, self._seed, commit)

    @property
    def seed(self) -> str:
        """The commit this run started from — what `revert()` returns to."""
        return self._seed

    @property
    def checkpoint_ref(self) -> str:
        return f"{CHECKPOINT_REF_PREFIX}{self.run_id}"

    def remove(self, *, keep_branch: bool = False) -> None:
        shutil.rmtree(self._home, ignore_errors=True)  # this run's HOME goes with it
        rm = _git(self.repo, "worktree", "remove", "--force", str(self.root))
        if rm.returncode != 0 and self.root.exists():
            # `worktree remove` refuses when `<root>/.git` no longer points back at the gitdir.
            # The agent can't write that file, but gate code runs as our uid and can, and
            # cleanup has to succeed either way. Restore the pointer we recorded and retry.
            with contextlib.suppress(OSError):
                (self.root / ".git").write_text(f"gitdir: {self._git_dir}\n")
            rm = _git(self.repo, "worktree", "remove", "--force", str(self.root))
        if not keep_branch:
            _git(self.repo, "branch", "-D", self.branch)
        _git(self.repo, "worktree", "prune")
        if rm.returncode != 0 and self.root.exists():
            raise WorktreeError(f"worktree remove failed: {rm.stderr.strip()}")
