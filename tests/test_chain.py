"""The hash chain, the provenance gate, and offline replay.

These test the claim, not the getters: a record edited after the fact cannot verify, and a
run whose record doesn't verify cannot teach memory anything.
"""

import json
import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from self_improving_coding_agent.contracts import GENESIS_HASH, BlockType, NodeState
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.recorder import RunRecorder

RUN = "run-abc123"


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


def _healthy_run(ledger: Ledger, run_id: str = RUN) -> None:
    """The block sequence a clean run produces."""
    ledger.append_block(run_id, BlockType.RUN_START, {"ticket_id": "t1"})
    ledger.append_block(run_id, BlockType.NODE_ATTEMPT, {"node": "discover"})
    ledger.append_block(run_id, BlockType.TOOL_CALL, {"tool": "read_file"})
    ledger.append_block(run_id, BlockType.VERDICT, {"node": "discover"})
    ledger.append_block(run_id, BlockType.ACCEPTANCE_GATE, {"exit_code": 0, "passed": True})


def _raw_edit(ledger: Ledger, sql: str, params: tuple) -> None:
    """Reach past the API and edit stored rows, the way a tamperer would."""
    conn = sqlite3.connect(ledger.db_path)
    with conn:
        conn.execute(sql, params)
    conn.close()


# ---- chain shape ---------------------------------------------------------------


def test_first_block_starts_from_genesis(tmp_path):
    block = _ledger(tmp_path).append_block(RUN, BlockType.RUN_START)
    assert block.seq == 0
    assert block.prev_hash == GENESIS_HASH
    assert len(block.content_hash) == 64


def test_each_block_links_to_the_previous_one(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    chain = ledger.blocks(RUN)
    assert [b.seq for b in chain] == [0, 1, 2, 3, 4]
    for earlier, later in zip(chain, chain[1:], strict=False):
        assert later.prev_hash == earlier.content_hash


def test_a_clean_chain_verifies(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    status = ledger.verify_chain(RUN)
    assert status.valid
    assert status.length == 5
    assert status.broken_at is None


def test_chains_are_per_run_so_concurrent_tickets_do_not_clobber(tmp_path):
    # Interleaved appends: a global chain head would entangle these two runs.
    ledger = _ledger(tmp_path)
    for _ in range(3):
        ledger.append_block("run-a", BlockType.NODE_ATTEMPT, {"node": "a"})
        ledger.append_block("run-b", BlockType.NODE_ATTEMPT, {"node": "b"})

    assert ledger.verify_chain("run-a").valid
    assert ledger.verify_chain("run-b").valid
    assert [b.seq for b in ledger.blocks("run-a")] == [0, 1, 2]
    assert [b.seq for b in ledger.blocks("run-b")] == [0, 1, 2]


# ---- tamper evidence ----------------------------------------------------------


def test_editing_a_stored_payload_breaks_the_chain(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)

    # Rewrite history: the gate failed, but the record now says it passed.
    _raw_edit(
        ledger,
        "UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = ?",
        (json.dumps({"exit_code": 1, "passed": False}), RUN, 4),
    )

    status = ledger.verify_chain(RUN)
    assert not status.valid
    assert status.broken_at == 4
    assert "altered" in (status.reason or "")


def test_editing_an_early_block_is_still_caught(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)

    _raw_edit(
        ledger,
        "UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = ?",
        (json.dumps({"ticket_id": "someone-elses-ticket"}), RUN, 0),
    )

    status = ledger.verify_chain(RUN)
    assert not status.valid
    assert status.broken_at == 0


def test_deleting_a_middle_block_is_caught_as_a_gap(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)

    # Excising an inconvenient step — say the tool call that read something it shouldn't.
    _raw_edit(ledger, "DELETE FROM blocks WHERE run_id = ? AND seq = ?", (RUN, 2))

    status = ledger.verify_chain(RUN)
    assert not status.valid
    assert "gap" in (status.reason or "")


def test_a_forged_block_with_a_recomputed_hash_still_breaks_the_link(tmp_path):
    # The hardest case: the tamperer knows the hash function and fixes up the block's own
    # content_hash. The *next* block's prev_hash no longer matches, so the chain still fails.
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    target = ledger.blocks(RUN)[2]
    from self_improving_coding_agent.ledger import block_hash

    forged_payload = {"tool": "totally_innocent"}
    forged_hash = block_hash(
        run_id=target.run_id,
        seq=target.seq,
        block_type=target.block_type,
        payload=forged_payload,
        git_hash=target.git_hash,
        created_at=target.created_at,
        prev_hash=target.prev_hash,
    )
    _raw_edit(
        ledger,
        "UPDATE blocks SET payload_json = ?, content_hash = ? WHERE run_id = ? AND seq = ?",
        (json.dumps(forged_payload), forged_hash, RUN, 2),
    )

    status = ledger.verify_chain(RUN)
    assert not status.valid
    assert status.broken_at == 3
    assert "link" in (status.reason or "")


# ---- payload hygiene ----------------------------------------------------------


def test_secrets_in_a_payload_are_scrubbed_and_the_chain_still_verifies(tmp_path):
    # Hashing has to happen after scrubbing, or every chain would fail on its own redactions.
    ledger = _ledger(tmp_path)
    ledger.append_block(
        RUN, BlockType.TOOL_CALL, {"input": {"cmd": "deploy --token=SUPERSECRETVALUE123"}}
    )

    stored = json.dumps(ledger.blocks(RUN)[0].payload)
    assert "SUPERSECRETVALUE123" not in stored
    assert ledger.verify_chain(RUN).valid


def test_a_huge_payload_field_is_bounded(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_block(RUN, BlockType.TOOL_CALL, {"output": "x" * 500_000})
    assert len(ledger.blocks(RUN)[0].payload["output"]) < 10_000


# ---- the provenance gate ------------------------------------------------------


def test_a_verified_run_may_teach_memory(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    assert ledger.provenance(RUN).allowed


def test_a_verified_failure_may_still_teach_memory(tmp_path):
    # The valuable lessons come from honest failures; only unverifiable runs are refused.
    ledger = _ledger(tmp_path)
    ledger.append_block(RUN, BlockType.RUN_START, {"ticket_id": "t1"})
    ledger.append_block(RUN, BlockType.VERDICT, {"node": "verify"})
    ledger.append_block(RUN, BlockType.ACCEPTANCE_GATE, {"exit_code": 1, "passed": False})

    decision = ledger.provenance(RUN)
    assert decision.allowed


def test_a_breaker_trip_refuses_the_lesson(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_block(RUN, BlockType.RUN_START, {"ticket_id": "t1"})
    ledger.append_block(RUN, BlockType.NODE_ATTEMPT, {"node": "implement"})
    ledger.append_block(RUN, BlockType.BREAKER_TRIP, {"node": "implement"})

    decision = ledger.provenance(RUN)
    assert not decision.allowed
    assert "breaker" in decision.reason
    assert "implement" in decision.reason


def test_a_tampered_chain_refuses_the_lesson(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    _raw_edit(ledger, "DELETE FROM blocks WHERE run_id = ? AND seq = ?", (RUN, 1))

    decision = ledger.provenance(RUN)
    assert not decision.allowed
    assert "integrity" in decision.reason


def test_an_unrecorded_run_refuses_the_lesson(tmp_path):
    # No record at all is not "nothing went wrong" — it is no evidence.
    decision = _ledger(tmp_path).provenance("run-never-happened")
    assert not decision.allowed


# ---- the recorder -------------------------------------------------------------


def test_node_lifecycle_states_become_the_right_blocks(tmp_path):
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, RUN)
    for state in (NodeState.RUNNING, NodeState.REDO, NodeState.COMPLETE, NodeState.FAILED):
        recorder.record_status({"node": "implement", "state": str(state)})

    kinds = [b.block_type for b in ledger.blocks(RUN)]
    assert kinds == [
        BlockType.NODE_ATTEMPT,
        BlockType.NODE_ATTEMPT,
        BlockType.VERDICT,
        BlockType.BREAKER_TRIP,
    ]


def test_a_failed_node_is_what_denies_provenance(tmp_path):
    # The wiring that matters: graph.py emits FAILED when the breaker trips, the recorder
    # turns that into a block, and the gate reads it. Nothing here parses agent text.
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, RUN)
    recorder.record_status({"node": "implement", "state": str(NodeState.RUNNING)})
    assert ledger.provenance(RUN).allowed

    recorder.record_status({"node": "implement", "state": str(NodeState.FAILED)})
    assert not ledger.provenance(RUN).allowed


def test_the_status_callback_passes_events_through(tmp_path):
    seen = []
    recorder = RunRecorder(_ledger(tmp_path), RUN)
    callback = recorder.status_callback(seen.append)
    callback({"node": "discover", "state": str(NodeState.RUNNING)})
    assert len(seen) == 1


def test_recording_never_raises_into_a_run(tmp_path):
    # A broken ledger must degrade to "no evidence" (which the gate refuses), not crash work.
    broken = _ledger(tmp_path)
    _raw_edit(broken, "DROP TABLE blocks", ())
    recorder = RunRecorder(broken, RUN)

    recorder.record_status({"node": "discover", "state": str(NodeState.RUNNING)})
    recorder.append(BlockType.RUN_END, {"outcome": "success"})


def test_blocks_carry_the_git_hash_they_were_written_at(tmp_path):
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, RUN)
    recorder.track_git("a" * 40)
    recorder.append(BlockType.RUN_START)
    assert ledger.blocks(RUN)[0].git_hash == "a" * 40


# ---- offline replay -----------------------------------------------------------


def test_replay_verifies_a_clean_chain_and_exits_zero(tmp_path, monkeypatch, capsys):
    from self_improving_coding_agent import cli

    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    monkeypatch.setattr(cli, "Ledger", lambda _path: ledger)

    assert cli.main(["replay", RUN]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out
    assert "may teach memory" in out


def test_replay_reports_a_broken_chain_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    from self_improving_coding_agent import cli

    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    _raw_edit(
        ledger,
        "UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = ?",
        (json.dumps({"passed": True}), RUN, 4),
    )
    monkeypatch.setattr(cli, "Ledger", lambda _path: ledger)

    assert cli.main(["replay", RUN]) == 1
    out = capsys.readouterr().out
    assert "BROKEN" in out
    assert "CHAIN BROKEN HERE" in out


def test_replay_of_an_unknown_run_exits_nonzero(tmp_path, monkeypatch, capsys):
    from self_improving_coding_agent import cli

    monkeypatch.setattr(cli, "Ledger", lambda _path: _ledger(tmp_path))
    assert cli.main(["replay", "run-nope"]) == 1
    assert "no chain recorded" in capsys.readouterr().out


# ---- properties ---------------------------------------------------------------


@given(
    kinds=st.lists(st.sampled_from(list(BlockType)), min_size=1, max_size=25),
    payload=st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(st.text(max_size=40), st.integers(), st.booleans(), st.none()),
        max_size=4,
    ),
)
@settings(deadline=None, max_examples=40)
def test_any_recorded_sequence_verifies(tmp_path_factory, kinds, payload):
    ledger = Ledger(tmp_path_factory.mktemp("chain") / "ledger.db")
    for kind in kinds:
        ledger.append_block(RUN, kind, dict(payload))

    status = ledger.verify_chain(RUN)
    assert status.valid, status.reason
    assert status.length == len(kinds)


@given(target=st.integers(min_value=0, max_value=4))
@settings(deadline=None, max_examples=5)
def test_tampering_with_any_block_is_detected(tmp_path_factory, target):
    ledger = Ledger(tmp_path_factory.mktemp("tamper") / "ledger.db")
    _healthy_run(ledger)

    _raw_edit(
        ledger,
        "UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = ?",
        (json.dumps({"tampered": True}), RUN, target),
    )

    assert not ledger.verify_chain(RUN).valid
    assert not ledger.provenance(RUN).allowed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


# ---- what a chain can and cannot detect ---------------------------------------
# Each of these reproduces a hole adversarial review found, so the fix stays fixed.


def test_truncating_the_tail_does_not_pass_as_clean(tmp_path):
    # The tail is where the breaker trip and the outcome live, so lopping it off is the
    # cheapest useful edit. Links alone can't see it — the recorded head can.
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    _raw_edit(ledger, "DELETE FROM blocks WHERE run_id = ? AND seq >= ?", (RUN, 3))

    status = ledger.verify_chain(RUN)
    assert not status.valid
    assert "truncated" in (status.reason or "")
    assert not ledger.provenance(RUN).allowed


def test_truncation_that_removes_a_breaker_trip_still_refuses_the_lesson(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_block(RUN, BlockType.RUN_START, {"ticket_id": "t1"})
    ledger.append_block(RUN, BlockType.BREAKER_TRIP, {"node": "implement"})
    ledger.append_block(RUN, BlockType.RUN_END, {"outcome": "failure"})

    _raw_edit(ledger, "DELETE FROM blocks WHERE run_id = ? AND seq >= ?", (RUN, 1))

    assert not ledger.provenance(RUN).allowed


def test_a_chain_with_no_recorded_head_is_not_trusted(tmp_path):
    ledger = _ledger(tmp_path)
    _healthy_run(ledger)
    _raw_edit(ledger, "DELETE FROM chain_heads WHERE run_id = ?", (RUN,))

    assert not ledger.verify_chain(RUN).valid


def test_the_head_tracks_the_chain_as_it_grows(tmp_path):
    ledger = _ledger(tmp_path)
    for expected_length in (1, 2, 3):
        block = ledger.append_block(RUN, BlockType.NODE_ATTEMPT, {"node": "n"})
        assert ledger.head(RUN) == (expected_length, block.content_hash)


def test_a_dropped_write_is_counted_not_swallowed(tmp_path):
    # An omitted block leaves no gap, so the chain still verifies. Silence must not read
    # as health: the recorder reports its own drops instead.
    broken = _ledger(tmp_path)
    _raw_edit(broken, "DROP TABLE blocks", ())
    recorder = RunRecorder(broken, RUN)

    recorder.append(BlockType.BREAKER_TRIP, {"node": "implement"})

    assert recorder.drops == 1
    assert not recorder.intact


def test_an_intact_recorder_reports_no_drops(tmp_path):
    recorder = RunRecorder(_ledger(tmp_path), RUN)
    recorder.append(BlockType.RUN_START)
    assert recorder.intact


# ---- payload hygiene, the parts a replay auditor relies on --------------------


def test_control_characters_cannot_rewrite_the_replay_verdict(tmp_path):
    # replay prints payloads above its VERIFIED/BROKEN line. A carriage return or a cursor
    # -up sequence in untrusted text could scroll back over that verdict, so they never
    # survive into a block in the first place.
    ledger = _ledger(tmp_path)
    ledger.append_block(RUN, BlockType.TOOL_CALL, {"note": "clean\r\x1b[2K\x1b[Aforged"})

    stored = ledger.blocks(RUN)[0].payload["note"]
    assert "\r" not in stored
    assert "\x1b" not in stored
    assert "\n" not in stored
    assert ledger.verify_chain(RUN).valid


def test_defanging_happens_after_scrubbing_so_multiline_secrets_still_match(tmp_path):
    ledger = _ledger(tmp_path)
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc123+/xyz\n-----END RSA PRIVATE KEY-----"
    ledger.append_block(RUN, BlockType.TOOL_CALL, {"blob": key})

    stored = ledger.blocks(RUN)[0].payload["blob"]
    assert "MIIEabc123" not in stored
    assert "REDACTED_PRIVATE_KEY" in stored


def test_file_content_is_digested_rather_than_stored(tmp_path):
    # write_file's content argument is arbitrary target-repo text. A durable audit row that
    # replay prints is the wrong home for it — a repo secret matching no scrub shape would
    # land there. Size and digest keep the audit value without the sink.
    from self_improving_coding_agent.recorder import _record_args

    secret_body = "internal_api_key_with_no_recognisable_shape_9182736450"
    args = _record_args("write_file", {"path": "app.py", "content": secret_body})

    assert args["path"] == "app.py"
    assert secret_body not in args["content"]
    assert "sha256:" in args["content"]
    assert str(len(secret_body)) in args["content"]


def test_an_unknown_tool_records_only_its_argument_names(tmp_path):
    from self_improving_coding_agent.recorder import _record_args

    args = _record_args("some_new_tool", {"payload": "sensitive", "target": "/etc/passwd"})
    assert args == {"arg_names": ["payload", "target"]}


def test_a_digest_changes_when_the_content_changes(tmp_path):
    # The audit question the digest has to answer: did the agent write something different
    # on its second attempt?
    from self_improving_coding_agent.recorder import _record_args

    first = _record_args("write_file", {"path": "a.py", "content": "x = 1"})
    second = _record_args("write_file", {"path": "a.py", "content": "x = 2"})
    assert first["content"] != second["content"]
