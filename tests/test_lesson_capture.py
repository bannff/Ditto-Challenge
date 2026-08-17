"""What reaches memory is the rule, not the agent's closing turn.

This is the self-improvement dimension's failure mode, and it is quiet: the run succeeds,
the report carries a lesson, the ledger records LESSON_WRITE, and every existing assertion
about "a lesson was stored" passes. Only the *content* was wrong, so nothing caught it.
The observed lesson began "Good - I've recalled the existing lessons and loaded the
lesson-writing skill..." and buried the rule 200 words down; later runs then recalled that
preamble as if it were guidance.

Root cause was not the prompt. Across every recorded run the learn swarm terminated after a
single agent, so the refiner and critic whose prompts said "output only the lesson" never
ran, and whichever agent went first became the whole node. The fix states the node's answer
as a schema (`NodeConfig.output_model`) so the shape does not depend on which agent finishes.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from strands.models.model import Model

from _doubles import FakeModel
from self_improving_coding_agent import workflow
from self_improving_coding_agent.agent_plane import build_agent, build_node_agents
from self_improving_coding_agent.contracts import (
    BlockType,
    LessonDraft,
    Outcome,
    Ticket,
    Verdict,
)
from self_improving_coding_agent.graph import (
    WorkflowResult,
    _extract_output,
    _run_swarm,
    _terminal_agent_result,
)
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.node import AgentSpec, NodeConfig
from self_improving_coding_agent.nodes import build_reference_nodes
from self_improving_coding_agent.workflow import _lesson_content


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "app.py").write_text("x = 1\n")
    (path / "test_app.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path

CHATTY = (
    "Good - I've recalled the existing lessons and loaded the lesson-writing skill. I can "
    "see there are already two related lessons about off-by-one boundary bugs. Now I need "
    "to draft a distinct lesson.\n\nHere's my durable lesson:\n\n---\n\n## Lesson: Clear "
    "docstrings enable single-pass bug fixes"
)
RULE = "When a docstring states the intended boundary, trust it over the implementation."


def _agent_result(text: str, structured=None):
    """Enough of an AgentResult for the accessors: they use str() and .structured_output."""

    class _R:
        structured_output = structured

        def __str__(self) -> str:
            return text

    return _R()


def _swarm_result(history: list[str], results: dict[str, object]):
    """A SwarmResult stand-in. `results` is ordered by *first* execution, as the SDK's is."""
    return SimpleNamespace(
        node_history=[SimpleNamespace(node_id=name) for name in history],
        results={
            name: SimpleNamespace(get_agent_results=lambda r=r: [r])
            for name, r in results.items()
        },
    )


def test_the_rule_is_persisted_and_the_closing_turn_is_not():
    """The regression. Both fields are present and they disagree; the schema wins."""
    wf = WorkflowResult(final_output=CHATTY, final_structured=LessonDraft(rule=RULE))
    content = _lesson_content(wf)
    assert content == RULE
    assert content is not None and "recalled the existing lessons" not in content


def test_a_node_that_ignored_its_schema_teaches_nothing_rather_than_teaching_junk():
    """Falling back to the prose is how the preamble got in. Storing nothing is honest;
    storing a transcript pollutes every later recall and never expires."""
    assert _lesson_content(WorkflowResult(final_output=CHATTY, final_structured=None)) is None


@pytest.mark.parametrize("rule", [None, "", "tiny"])
def test_a_run_with_no_usable_rule_does_not_claim_to_have_learned(tmp_path, rule):
    """"No rule" has to be a stated reason not to teach, not something that happens to be
    true. Today every path that loses the schema also degrades the run, so a placeholder
    would be unreachable — but only by coincidence of two distant mechanisms, and
    `tests/test_safety.py` already builds the undegraded-but-schemaless shape by hand.
    """
    memory = MagicMock()
    memory.retrieve.return_value = []
    ledger = Ledger(tmp_path / "ledger.db")
    wf = WorkflowResult(
        verdicts=[Verdict(node="discover", passed=True)],
        final_output=CHATTY,
        final_structured=LessonDraft(rule=rule) if rule is not None else None,
        outcome=Outcome.SUCCESS,
        degraded=False,  # the run itself was fine; only the lesson is missing
    )
    repo = _init_repo(tmp_path / "repo")
    ticket = Ticket(
        id="t-norule",
        repository=str(repo),
        request="Fix the off-by-one in the reorder boundary helper.",
        acceptance_command="pytest test_app.py",
    )
    with patch.object(workflow, "run_workflow", return_value=wf), patch.object(
        workflow, "setup_telemetry"
    ):
        # run_workflow is mocked, so no model call happens — but default_models() still
        # builds real boto3 sessions if models= is omitted, and a runner with no AWS
        # profile named "default" fails there before the mock is ever reached.
        report = workflow.run_ticket(
            ticket, models=cast(Any, object()), memory=memory, ledger=ledger
        )

    memory.store.assert_not_called()
    assert report.lesson is None
    refused = next(
        b for b in ledger.blocks(report.run_id) if b.block_type == BlockType.LESSON_REFUSED
    )
    assert refused.payload["rule_produced"] is False
    assert "no usable rule" in refused.payload["reason"]
    assert BlockType.LESSON_WRITE not in [b.block_type for b in ledger.blocks(report.run_id)]


@pytest.mark.parametrize(
    "rule", ["   padded rule about boundaries   ", "\n a rule with edges \t"]
)
def test_the_stored_rule_is_stripped(rule: str):
    wf = WorkflowResult(final_structured=LessonDraft(rule=rule))
    assert _lesson_content(wf) == rule.strip()


@pytest.mark.parametrize("bad", ["", "too short", "x" * 5_000, "   ", "\n\t"])
def test_a_rule_that_is_not_one_is_rejected_at_our_boundary_not_by_the_schema(bad: str):
    """The schema must accept anything; the policy is applied where a bad value is free.

    This is the shape of a real safety bug, not a style choice. With `min_length`/`max_length`
    on the contract, a model that returned a violating value was asked for it again with
    forced mode still latched and only the schema tool on offer — the SDK turns a
    ValidationError into a tool *error*, not an exception, so the event loop recursed until
    wall-clock. Measured at 312 model calls and a RecursionError, times `max_redos`, against
    a hard requirement that runs stay bounded. A length cap is precisely what a chatty model
    violates repeatedly.
    """
    draft = LessonDraft(rule=bad)  # must not raise: raising here is what loops
    content = _lesson_content(WorkflowResult(final_structured=draft))
    assert content is None or len(content) <= 600


def test_an_over_long_rule_is_truncated_rather_than_lost():
    long_rule = "Always verify the boundary case. " * 100
    content = _lesson_content(WorkflowResult(final_structured=LessonDraft(rule=long_rule)))
    assert content is not None and len(content) == 600


def test_internal_whitespace_is_collapsed_before_storage():
    """Every stored rule is embedded and searched on every later run, so a rule wrapped
    across lines must not read as a different rule from the same text on one line."""
    wrapped = LessonDraft(rule="Trust the docstring\n   over  the\timplementation always.")
    assert (
        _lesson_content(WorkflowResult(final_structured=wrapped))
        == "Trust the docstring over the implementation always."
    )


def test_the_learn_node_declares_the_schema():
    """Without this the whole mechanism is inert, and inert looks identical in the report."""
    nodes = {n.name: n for n in build_reference_nodes(worktree_tools=[], policy_tool=lambda q: q)}
    assert nodes["learn"].output_model is LessonDraft
    # And no other node claims it: a schema on `implement` would force prose into a rule.
    assert [n for n, c in nodes.items() if c.output_model is not None] == ["learn"]


def test_the_schema_reaches_the_agent_the_sdk_will_force_it_on():
    """`output_model` is node-scoped, so it must land on *every* agent: the swarm is free to
    stop anywhere, and in practice it stops at its entry point."""
    spec = AgentSpec(name="drafter", system_prompt="draft")
    assert build_agent(spec, model=FakeModel())._default_structured_output_model is None
    agent = build_agent(spec, model=FakeModel(), output_model=LessonDraft)
    assert agent._default_structured_output_model is LessonDraft


def test_the_terminal_agent_is_the_one_that_spoke_last_not_the_one_that_started_last():
    """`results` is keyed by agent name and overwritten in place, so its order is order of
    *first* execution. On a revisit ("drafter, refiner, critic, refiner") reading it hands
    back the critic's text while the refiner is the agent that actually finished.
    """
    result = _swarm_result(
        history=["drafter", "refiner", "critic", "refiner"],
        results={
            "drafter": _agent_result("draft"),
            "refiner": _agent_result("final answer", LessonDraft(rule=RULE)),
            "critic": _agent_result("handing back to the refiner"),
        },
    )
    assert str(_terminal_agent_result(result)) == "final answer"
    text, structured = _extract_output(result)
    assert text == "final answer"
    assert isinstance(structured, LessonDraft) and structured.rule == RULE


def test_the_text_and_the_structured_value_come_from_the_same_turn():
    """The evaluators judge the text. A structured value lifted from a different agent would
    be scored against prose it does not correspond to."""
    result = _swarm_result(
        history=["a", "b"],
        results={
            "a": _agent_result("first", LessonDraft(rule="A rule from the wrong agent entirely")),
            "b": _agent_result("second", LessonDraft(rule=RULE)),
        },
    )
    text, structured = _extract_output(result)
    assert text == "second"
    assert isinstance(structured, LessonDraft) and structured.rule == RULE


def test_a_swarm_with_no_history_is_still_readable():
    """Fail soft: an empty history must not lose an answer that exists."""
    result = _swarm_result(history=[], results={"only": _agent_result("the answer")})
    text, structured = _extract_output(result)
    assert text == "the answer"
    assert structured is None


def test_a_node_without_a_schema_reports_no_structured_value():
    result = _swarm_result(history=["a"], results={"a": _agent_result("prose only")})
    assert _extract_output(result) == ("prose only", None)


class _ChattyThenCompliantModel(Model):
    """A model that behaves the way the real one did: ends its turn with narration.

    The SDK answers that by *forcing* the schema tool on a second pass (`end_turn` +
    `structured_output_context.is_enabled`), which is the load-bearing behaviour this fix
    depends on and the one a mock of our own code cannot demonstrate. The forced tool is
    named for the schema class, with `tool_choice={"any": {}}`.
    """

    def __init__(self, rule: str) -> None:
        self.rule = rule
        self.turns = 0

    def update_config(self, **model_config: Any) -> None: ...

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "chatty"}

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs
    ) -> AsyncGenerator[Any, None]:
        # Never called on this path: the agent-level schema is satisfied by the forced tool
        # call in `stream`, not by the model's own structured_output entry point. Asserting
        # that is the point — it is what proves the forcing path is the one under test.
        raise AssertionError("the forced tool path should have produced the value")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, **kwargs
    ) -> AsyncIterable[Any]:
        self.turns += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.turns == 1:
            yield {"contentBlockDelta": {"delta": {"text": CHATTY}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "1", "name": LessonDraft.__name__}}
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({"rule": self.rule})}}
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


def test_the_sdk_forces_the_schema_when_the_agent_answers_with_narration():
    """End to end through the real event loop: narration on turn one, parsed rule anyway."""
    model = _ChattyThenCompliantModel(RULE)
    agent = build_agent(
        AgentSpec(name="drafter", system_prompt="draft"),
        model=model,
        output_model=LessonDraft,
    )
    result = asyncio.run(agent.invoke_async("summarise the run"))
    assert model.turns == 2, "the SDK should have forced a second pass"
    assert isinstance(result.structured_output, LessonDraft)
    assert result.structured_output.rule == RULE


def test_a_learn_swarm_of_narrating_agents_still_yields_a_clean_rule():
    """The whole chain, offline: node data -> agents -> real Swarm -> _extract_output.

    The swarm terminating at its entry point is not a failure mode here, it is the observed
    normal, so the entry agent alone has to be able to satisfy the node's contract.
    """
    node = NodeConfig(
        name="learn",
        agents=[AgentSpec(name="drafter", system_prompt="draft")],
        output_model=LessonDraft,
        execution_timeout=30.0,
        node_timeout=30.0,
    )
    agents = build_node_agents(node, {"builder": _ChattyThenCompliantModel(RULE)})
    (text, structured), _ = asyncio.run(_run_swarm(agents, node, "summarise the run"))
    assert isinstance(structured, LessonDraft) and structured.rule == RULE
    # The narration does not survive into the node's text either: `AgentResult.__str__`
    # returns the schema JSON once a structured value exists. So the evaluators judge the
    # rule too, which is why the lesson_shape rubric is worded for a JSON object.
    assert "recalled the existing lessons" not in text
    assert json.loads(text) == {"rule": RULE}
    assert _lesson_content(WorkflowResult(final_output=text, final_structured=structured)) == RULE
