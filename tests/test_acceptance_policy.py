"""The acceptance policy contract: `validate()` / `normalize()`, no worktree, no network.

`acceptance_command` is untrusted ticket text our own code executes, so this is the
argv-level trust boundary. Tests are pure and table-driven — adding a case is adding a row.

The last section holds properties that were once real escapes (an interpreter accepting a
script, `-m unittest <dotted.name>` importing from outside the jail, a bare interpreter or
collect-only run passing as a gate, an unencodable path crashing instead of refusing). They
are regression guards now.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from self_improving_coding_agent.acceptance_policy import (
    POLICIES,
    AcceptanceRejected,
    normalize,
    resolve,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "examples" / "tickets"


@pytest.fixture
def jail(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    return root


def _check(command: str, jail: Path | None) -> None:
    validate(normalize(shlex.split(command)), in_jail=jail)


def _refusal(command: str, jail: Path | None = None) -> str:
    with pytest.raises(AcceptanceRejected) as e:
        _check(command, jail)
    return str(e.value)


# --- allowed forms -------------------------------------------------------------------------

ALLOWED = [
    "pytest",
    "pytest -q",
    "py.test -q",
    # --co is deliberately absent: see the collect-only FINDING below.
    "pytest -x -v --no-header -ra -s --quiet --verbose --exitfirst",
    "pytest tests",
    "pytest tests/test_x.py",
    "pytest tests/test_x.py::test_x",
    "pytest -q tests/test_x.py",
    "pytest .",
    # both spellings of a valued flag
    "pytest --maxfail=1",
    "pytest --maxfail 1",
    "pytest --tb=short",
    "pytest -k not_slow",
    "pytest -k 'not slow'",
    "pytest -m slow",
    "pytest -m=slow",
    # the interpreter's only sanctioned job
    "python -m pytest",
    "python3 -m pytest -q tests",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_legitimate_gate_commands_are_allowed(command: str, jail: Path):
    _check(command, jail)  # must not raise


def test_every_seed_ticket_still_passes_the_policy(jail: Path):
    # Regression guard: the shipped tickets are the commands a reviewer will actually run.
    commands = [
        json.loads(p.read_text())["acceptance_command"]
        for p in sorted(TICKETS.glob("*.json"))
    ]
    declared = [c for c in commands if c]
    assert len(declared) >= 7, "expected the seed tickets to declare gates"
    for command in declared:
        _check(command, jail)


# --- refused forms -------------------------------------------------------------------------

REFUSED_RUNNER = [
    "make test",  # a shell interpreter in disguise (-C, -f)
    "make -C /tmp all",
    "tox",  # fetches from the network
    "go test ./...",
    "cargo test",
    "npm test",
    "npx jest",
    "yarn test",
    "bash -c id",
    "sh",
    "./configure",
    "/usr/bin/python3 -m pytest",  # only the bare names are policy keys
    "env pytest",
    "sudo pytest",
    "curl http://evil",
]


@pytest.mark.parametrize("command", REFUSED_RUNNER)
def test_only_allowlisted_runners_are_accepted(command: str, jail: Path):
    assert "runner not allowed" in _refusal(command, jail)


REFUSED_FLAG = [
    "pytest --rootdir=/",  # would move the collection root out of the worktree
    "pytest -p no:cacheprovider",  # plugin loading
    "pytest -p evil_plugin",
    "pytest --import-mode=importlib",
    "pytest --basetemp=/tmp/x",
    "pytest -c /etc/pytest.ini",
    "pytest --co=x",  # a bare flag given a value is not the same flag
    "pytest -svv",  # bundled short flags are not decomposed
    "pytest --",  # end-of-options is not a pass-through
    "pytest -- ../../",
    "pytest ---q",
    "python3 -m unittest discover -s /etc -t /etc",  # -s/-t are not interpreter flags
    "python3 -X importtime -m pytest",
    "python3 -c print(1)",
    "python3 -mpytest",  # attached form bypasses the module check, so it must be refused
]


@pytest.mark.parametrize("command", REFUSED_FLAG)
def test_unrecognised_flags_are_refused_not_passed_through(command: str, jail: Path):
    assert "not allowed" in _refusal(command, jail)


@pytest.mark.parametrize("command", ["pytest -k", "pytest -m", "pytest --maxfail",
                                     "pytest --tb"])
def test_a_valued_flag_without_its_value_is_refused(command: str, jail: Path):
    assert "missing its value" in _refusal(command, jail)


@pytest.mark.parametrize("command", ["", "   ", "\t\n"])
def test_empty_command_is_refused(command: str, jail: Path):
    assert "empty command" in _refusal(command, jail)


# Only `python -m pytest` survives, and only because normalize() folds it onto the pytest
# row. Every other interpreter invocation is refused at the runner, which is what removes
# the whole `-m` module/marker ambiguity.
REFUSED_INTERPRETER = ["python3 -m pip", "python3 -m pip --version", "python -m http.server",
                       "python3 -m venv", "python3 -m=pip", "python -m this",
                       "python3 -m unittest.__main__", "python3 -m unittest",
                       "python3 -m unittest discover", "python3 gate.py", "python3",
                       "python", "python3 -m unittest this", "python -m unittest pip"]


@pytest.mark.parametrize("command", REFUSED_INTERPRETER)
def test_only_pytest_can_be_the_gate(command: str, jail: Path):
    assert "runner not allowed" in _refusal(command, jail)


# --- `-m` means two different things -------------------------------------------------------


ARGFILE = [
    "pytest @opts.txt",                 # the plain splice
    "pytest @opts.txt tests",
    "pytest tests @opts.txt",           # position doesn't matter: argparse expands all of argv
    "pytest -k @opts.txt tests",        # ...including a flag's value slot
    "pytest -m @opts.txt",
    "pytest --tb @opts.txt",
    "pytest --maxfail @opts.txt",
    "pytest @",                         # the bare prefix
    "pytest @nested.txt",               # expansion is recursive
    "python3 -m pytest @opts.txt",      # and survives normalization
    "python -m pytest -q @opts.txt tests",
]


@pytest.mark.parametrize("command", ARGFILE)
def test_an_argument_file_token_is_refused_anywhere_in_the_command(command: str, jail: Path):
    """pytest sets fromfile_prefix_chars="@", so argparse replaces such a token with the
    lines of that file *before* parsing — at any position, recursively. That splices in
    arbitrary options (a later `-o addopts=` overrides the one we force), and the file can be
    outside the worktree, so its contents leak through pytest's error output."""
    assert "argument-file token is not allowed" in _refusal(command, jail)


def test_an_attached_argfile_value_is_inert_but_still_refused(jail: Path):
    # `--tb=@f` is NOT expanded by argparse (only tokens whose first character is @), so it
    # would fail closed at the runner anyway — but the token still names a file we never want
    # near the gate, so the policy refuses it rather than relying on pytest's usage error.
    for command in ("pytest --tb=@opts.txt", "pytest -k=@opts.txt"):
        assert "argument-file token is not allowed" in _refusal(command, jail)


def test_dash_m_is_a_marker_expression_for_pytest_not_a_module(jail: Path):
    # `-m` means "marker expression" to pytest and "module" to an interpreter. Rather than
    # police both meanings, only pytest is a runner — so the ambiguity cannot arise, and a
    # marker expression that happens to look like a module name is just a marker.
    for command in ("pytest -m pip", "pytest -m not_slow", "pytest -m=pip",
                    "pytest -m 'not slow'"):
        _check(command, jail)
    assert "runner not allowed" in _refusal("python3 -m pip", jail)


def test_python_dash_m_pytest_normalizes_to_pytest():
    for command in ("python -m pytest -q", "python3 -m pytest -q"):
        assert normalize(shlex.split(command)) == ["pytest", "-q"]
    # unchanged when it isn't that exact shape
    assert normalize(["python3", "-m", "unittest"]) == ["python3", "-m", "unittest"]
    assert normalize(["pytest", "-q"]) == ["pytest", "-q"]
    assert normalize(["python3", "-m"]) == ["python3", "-m"]
    assert normalize([]) == []


def test_normalization_subjects_a_module_launch_to_pytests_own_rules(jail: Path):
    # Without the fold, pytest's escape flags would be validated as interpreter args.
    assert "flag not allowed for pytest" in _refusal("python -m pytest --rootdir=/", jail)
    assert "flag not allowed for pytest" in _refusal("python3 -m pytest -p evil", jail)
    assert "escapes the worktree" in _refusal("python3 -m pytest ../../tests", jail)


# --- the jail ------------------------------------------------------------------------------

ESCAPES = [
    "pytest /etc",
    "pytest /etc/passwd",
    "pytest ../../tests",
    "pytest ../",
    "pytest ..",
    "pytest tests/../..",
    "pytest ./tests/../../../etc",
    "pytest -q ../../../",
    "python3 -m pytest /etc",
]


@pytest.mark.parametrize("command", ESCAPES)
def test_a_positional_path_outside_the_worktree_is_refused(command: str, jail: Path):
    assert "escapes the worktree" in _refusal(command, jail)


def test_an_absolute_path_inside_the_worktree_is_allowed(jail: Path):
    _check(f"pytest {jail / 'tests'}", jail)
    _check(f"pytest {jail}", jail)


def test_the_executed_positional_is_the_path_we_validated(jail: Path):
    """pytest doesn't resolve symlinks, so it must be handed the canonical path rather than
    the ticket's spelling: an unresolved absolute path (macOS /tmp, or a symlinked worktree
    base) would otherwise sit outside our resolved --confcutdir and pull in an ancestor's
    conftest.py — the exact escape --confcutdir exists to prevent."""
    unresolved = Path(str(jail).replace("/private/tmp", "/tmp"))
    for spelling in (jail / "tests", unresolved / "tests", Path("tests")):
        argv = resolve(["pytest", str(spelling)], in_jail=jail)
        assert argv[-1] == "tests", f"{spelling} executed as {argv[-1]!r}"
    # the root itself, and a node id, keep their meaning
    assert resolve(["pytest", str(jail)], in_jail=jail)[-1] == "."
    assert resolve(["pytest", "tests/test_x.py::test_x"], in_jail=jail)[-1] == \
        "tests/test_x.py::test_x"


def test_an_existing_symlink_pointing_out_is_refused(jail: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (jail / "link_out").symlink_to(outside, target_is_directory=True)
    assert "escapes the worktree" in _refusal("pytest link_out", jail)
    assert "escapes the worktree" in _refusal("pytest link_out/test_evil.py", jail)


PATH_TOKENS = st.one_of(
    st.text(alphabet="./abz-_", min_size=0, max_size=12),
    st.sampled_from(["..", "../..", "/", "/etc/passwd", ".", "tests", "~", "~/.ssh/id_rsa",
                     "//etc", "./../.", "tests/../..", "a/../../b"]),
    st.lists(st.sampled_from(["..", ".", "a", "tests", "/"]), min_size=1, max_size=6)
      .map("/".join),
)


@settings(max_examples=300, deadline=None)
@given(token=PATH_TOKENS)
def test_no_positional_token_ever_resolves_outside_the_jail(tmp_path_factory, token: str):
    # The property the gate rests on: whatever the ticket says, an accepted positional path
    # points inside the worktree. Refusal is always an acceptable answer; silently
    # accepting a path that resolves outside never is.
    root = tmp_path_factory.mktemp("jail")
    try:
        validate(["pytest", token], in_jail=root)
    except AcceptanceRejected:
        return
    resolved = (Path(token) if Path(token).is_absolute() else root / token).resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents


@settings(max_examples=200, deadline=None)
@given(runner=st.text(max_size=12), tail=st.lists(st.text(max_size=8), max_size=4))
def test_a_runner_outside_the_policy_table_is_always_refused(runner: str, tail: list[str]):
    if runner in POLICIES:
        return
    with pytest.raises(AcceptanceRejected):
        validate([runner, *tail])


@settings(max_examples=200, deadline=None)
@given(flag=st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=10))
def test_an_unknown_flag_is_refused_for_every_runner(flag: str):
    token = f"-{flag}"
    for runner, policy in POLICIES.items():
        name = token.partition("=")[0]
        if name in policy.flags or name in policy.valued_flags or name == "-m":
            continue
        with pytest.raises(AcceptanceRejected):
            validate([runner, token])


# --- properties that were once holes; each of these is now enforced ------------------------


def test_an_interpreter_cannot_be_handed_a_script_to_run(jail: Path):
    (jail / "gate.py").write_text("print('everything is fine')\n")
    for command in ("python3 gate.py", "python gate.py", "python3 ./gate.py"):
        with pytest.raises(AcceptanceRejected):
            _check(command, jail)


@pytest.mark.parametrize("command", ["python3 -m unittest this",
                                     "python3 -m unittest self_improving_coding_agent.settings",
                                     "python -m unittest pip"])
def test_a_unittest_positional_cannot_name_a_module_outside_the_worktree(command: str,
                                                                        jail: Path):
    with pytest.raises(AcceptanceRejected):
        _check(command, jail)


def test_a_bare_interpreter_is_not_a_test_gate():
    for command in (["python"], ["python3"]):
        with pytest.raises(AcceptanceRejected):
            validate(command)


def test_a_collect_only_gate_is_refused_because_it_cannot_fail():
    for command in ("pytest --co", "pytest --collect-only -q", "python3 -m pytest --co"):
        with pytest.raises(AcceptanceRejected):
            _check(command, None)


@pytest.mark.parametrize("token", ["\x00", "tests/\x00", "\ud800"])
def test_an_unencodable_path_is_refused_not_a_crash(token: str, jail: Path):
    with pytest.raises(AcceptanceRejected):
        validate(["pytest", token], in_jail=jail)
