"""Cassette record/replay, and the boundaries that make it safe to exist.

The interesting tests are the refusals: recording is fixture-only because a cassette is
unredacted, replay fails loud rather than silently calling the live model, and a replayed
run can neither ship nor teach.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from _doubles import FakeModel
from self_improving_coding_agent.cassette import (
    HEADER,
    Cassette,
    CassetteError,
    RecordingCassetteModel,
    ReplayModel,
    cassette_key,
    model_wrapper,
    recording_allowed,
)
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.model_record import RecordingModel
from self_improving_coding_agent.recorder import RunRecorder

REPO_SOURCE = "ENCRYPTION_KEY = 'nQw8vZ2pL5xR7tY1'"
MESSAGES: list[Any] = [{"role": "user", "content": [{"text": f"file:\n{REPO_SOURCE}"}]}]


def _drain(model, messages=None, tool_specs=None, system_prompt="sys") -> list[dict]:
    async def collect() -> list[dict]:
        return [
            e async for e in model.stream(messages or MESSAGES, tool_specs, system_prompt)
        ]

    return asyncio.run(collect())


def _fixture_repo(tmp_path):
    root = tmp_path / "fixtures"
    repo = root / "target_app"
    repo.mkdir(parents=True)
    return root, repo


# ---- recording is refused outside a fixture root ------------------------------


def test_recording_is_off_when_no_fixture_root_is_configured(tmp_path):
    # Fail-closed: unset means off, so an operator can't accidentally record a real repo.
    reason = recording_allowed(tmp_path / "some_repo", None)
    assert reason is not None
    assert "unredacted" in reason


def test_recording_is_refused_for_a_repo_outside_the_fixture_root(tmp_path):
    root, _ = _fixture_repo(tmp_path)
    reason = recording_allowed(tmp_path / "elsewhere" / "real_repo", root)
    assert reason is not None
    assert "outside the fixture root" in reason


def test_recording_is_allowed_inside_the_fixture_root(tmp_path):
    root, repo = _fixture_repo(tmp_path)
    assert recording_allowed(repo, root) is None


def test_a_sibling_of_the_fixture_root_cannot_masquerade_as_inside_it(tmp_path):
    # A prefix-string check would accept "fixtures_evil" as inside "fixtures".
    root, _ = _fixture_repo(tmp_path)
    evil = tmp_path / "fixtures_evil"
    evil.mkdir()
    assert recording_allowed(evil, root) is not None


def test_open_for_record_refuses_and_writes_nothing(tmp_path):
    cassette = Cassette.for_run("run-x", tmp_path / "cassettes")
    with pytest.raises(CassetteError):
        cassette.open_for_record(tmp_path / "real_repo", None)
    assert not cassette.path.exists()


# ---- the cassette id is a path component -------------------------------------


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "", ".."])
def test_a_traversing_cassette_id_is_refused(tmp_path, bad):
    with pytest.raises(CassetteError):
        Cassette.for_run(bad, tmp_path / "cassettes")


def test_a_normal_cassette_id_lands_inside_the_directory(tmp_path):
    cassette = Cassette.for_run("run-abc123", tmp_path / "cassettes")
    assert cassette.path.parent == (tmp_path / "cassettes").resolve()


# ---- record then replay -------------------------------------------------------


def _recorded(tmp_path, inner=None) -> Cassette:
    root, repo = _fixture_repo(tmp_path)
    cassette = Cassette.for_run("run-rec", tmp_path / "cassettes").open_for_record(repo, root)
    _drain(RecordingCassetteModel(inner or FakeModel(text="recorded answer"), cassette))
    return cassette


def test_a_recorded_call_replays_the_same_events(tmp_path):
    _recorded(tmp_path)
    replay = ReplayModel(Cassette.for_run("run-rec", tmp_path / "cassettes").load())

    events = _drain(replay)

    assert events[1]["contentBlockDelta"]["delta"]["text"] == "recorded answer"
    assert events[-2]["messageStop"]["stopReason"] == "end_turn"


def test_replay_makes_no_call_to_the_inner_model(tmp_path):
    # The whole point: offline. ReplayModel holds no inner model at all, so there is nothing
    # it could fall back to.
    _recorded(tmp_path)
    replay = ReplayModel(Cassette.for_run("run-rec", tmp_path / "cassettes").load())
    assert not hasattr(replay, "_inner")
    assert replay.get_config()["model_id"] == "cassette-replay"


def test_a_cassette_miss_fails_loud_instead_of_calling_the_live_model(tmp_path):
    """A silent fallback would mean a 'replay' that quietly spent money and proved nothing."""
    _recorded(tmp_path)
    replay = ReplayModel(Cassette.for_run("run-rec", tmp_path / "cassettes").load())

    with pytest.raises(CassetteError) as excinfo:
        _drain(replay, [{"role": "user", "content": [{"text": "a different question"}]}])

    assert "diverged" in str(excinfo.value)


def test_repeated_identical_requests_replay_in_order(tmp_path):
    root, repo = _fixture_repo(tmp_path)
    cassette = Cassette.for_run("run-rep", tmp_path / "cassettes").open_for_record(repo, root)
    first, second = FakeModel(text="first"), FakeModel(text="second")
    _drain(RecordingCassetteModel(first, cassette))
    _drain(RecordingCassetteModel(second, cassette))

    replay = ReplayModel(Cassette.for_run("run-rep", tmp_path / "cassettes").load())

    assert _drain(replay)[1]["contentBlockDelta"]["delta"]["text"] == "first"
    assert _drain(replay)[1]["contentBlockDelta"]["delta"]["text"] == "second"


def test_a_cassette_is_bound_to_the_repo_it_was_recorded_against(tmp_path):
    # Recorded decisions reference paths and content that only make sense in their own repo.
    _recorded(tmp_path)
    other = tmp_path / "other_repo"
    other.mkdir()

    with pytest.raises(CassetteError) as excinfo:
        Cassette.for_run("run-rec", tmp_path / "cassettes").load(other)

    assert "recorded against" in str(excinfo.value)


def test_an_empty_cassette_is_refused(tmp_path):
    root, repo = _fixture_repo(tmp_path)
    Cassette.for_run("run-empty", tmp_path / "cassettes").open_for_record(repo, root)
    with pytest.raises(CassetteError):
        Cassette.for_run("run-empty", tmp_path / "cassettes").load()


def test_a_missing_cassette_is_refused(tmp_path):
    with pytest.raises(CassetteError):
        Cassette.for_run("run-nope", tmp_path / "cassettes").load()


# ---- the file itself ----------------------------------------------------------


def test_the_file_says_it_is_unredacted_and_is_owner_only(tmp_path):
    # No "scrubbed" label anywhere: the contents really are raw prompts, and a reassuring
    # label is how such a file ends up committed.
    cassette = _recorded(tmp_path)

    first_line = json.loads(cassette.path.read_text().splitlines()[0])
    assert first_line["header"] == HEADER
    assert "UNREDACTED" in HEADER
    assert cassette.path.stat().st_mode & 0o777 == 0o600


def test_the_prompt_is_never_written_to_disk_only_its_digest(tmp_path):
    """Replay needs to *recognise* a request, not reproduce it — so the prompt, with the repo
    source it quotes and the primed lessons it carries, never reaches the file."""
    cassette = _recorded(tmp_path)
    contents = cassette.path.read_text()

    assert REPO_SOURCE not in contents
    assert "nQw8vZ2pL5xR7tY1" not in contents
    assert cassette_key(MESSAGES, None, "sys") in contents  # the digest, and only that


def test_responses_are_stored_verbatim_because_replay_needs_fidelity(tmp_path):
    """The tradeoff stated as a test: a response holds the model's tool-use arguments, so
    written file bodies are in here. Scrubbing them would break replay, which is why the file
    is bounded to fixture repos instead of redacted."""
    root, repo = _fixture_repo(tmp_path)
    cassette = Cassette.for_run("run-fid", tmp_path / "cassettes").open_for_record(repo, root)
    _drain(RecordingCassetteModel(FakeModel(text="def fix(): return 42"), cassette))

    assert "def fix(): return 42" in cassette.path.read_text()


def test_bytes_in_a_stream_event_survive_the_round_trip(tmp_path):
    # reasoningContent.redactedContent is raw bytes, which JSON cannot hold.
    class Reasoning(FakeModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            yield {
                "contentBlockDelta": {
                    "delta": {"reasoningContent": {"redactedContent": b"\x00\x01raw"}}
                }
            }
            yield {"messageStop": {"stopReason": "end_turn"}}

    _recorded(tmp_path, Reasoning())
    events = _drain(ReplayModel(Cassette.for_run("run-rec", tmp_path / "cassettes").load()))

    delta = events[0]["contentBlockDelta"]["delta"]["reasoningContent"]
    assert delta["redactedContent"] == b"\x00\x01raw"


# ---- keying -------------------------------------------------------------------


def test_the_cassette_key_includes_the_system_prompt(tmp_path):
    """Unlike the chain's request_hash, which hashes the system prompt apart so growing
    memory doesn't read as divergence. Replay needs an exact match on everything sent."""
    assert cassette_key(MESSAGES, None, "sys A") != cassette_key(MESSAGES, None, "sys B")


def test_the_cassette_key_changes_with_the_conversation(tmp_path):
    other: list[Any] = [{"role": "user", "content": [{"text": "different"}]}]
    assert cassette_key(MESSAGES, None, "sys") != cassette_key(other, None, "sys")


# ---- the wrapper composition -------------------------------------------------


def test_record_mode_wraps_both_the_chain_and_the_cassette(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    recorder = RunRecorder(ledger, "run-w")
    cassette = _recorded(tmp_path)
    cassette.mode = "record"

    wrapped = model_wrapper(recorder.wrap_model, cassette)(FakeModel(), "implement", "builder")

    assert isinstance(wrapped, RecordingModel)  # chain digests outermost


def test_replay_mode_substitutes_the_model_entirely(tmp_path):
    _recorded(tmp_path)
    ledger = Ledger(tmp_path / "l.db")
    recorder = RunRecorder(ledger, "run-w")
    cassette = Cassette.for_run("run-rec", tmp_path / "cassettes").load()
    live = FakeModel()

    wrapped = model_wrapper(recorder.wrap_model, cassette)(live, "implement", "builder")

    # Chain recorder outermost so a replayed run leaves comparable digests; the live model
    # is gone from the chain entirely.
    assert isinstance(wrapped, RecordingModel)
    assert isinstance(wrapped._inner, ReplayModel)


def test_no_cassette_means_only_the_chain_wrapper(tmp_path):
    recorder = RunRecorder(Ledger(tmp_path / "l.db"), "run-w")
    live = FakeModel()

    wrapped = model_wrapper(recorder.wrap_model, None)(live, "implement", "builder")

    assert isinstance(wrapped, RecordingModel)
    assert wrapped._inner is live


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
