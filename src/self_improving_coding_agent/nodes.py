"""Reference coding-agent nodes — the swap-in layer that proves the plumbing.

Discover -> Implement -> Verify -> Learn. These are data (NodeConfigs) assembled from
run-scoped tools; on a different use case you write a different set and the engine is
untouched. Every node is a 3-agent swarm on three model families (builder / reviewer /
third) and carries one domain skill.

Each node runs out-of-box `strands_evals` judges in two roles. **Gating** judges answer
"did this node do its job" (goal success, faithfulness) and can fail it. **Informational**
judges rate individual tool calls and the trajectory — useful signal, but a few
unjustified reads in a long investigation isn't a failed node, so they never veto. Their
findings carry to the final node, which distils the run's lesson from process as well as
outcome. Thresholds are moderate on purpose.
"""

from __future__ import annotations

from pathlib import Path

from strands_evals.evaluators import (
    CoherenceEvaluator,
    ConcisenessEvaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    OutputEvaluator,
    ResponseRelevanceEvaluator,
    ToolParameterAccuracyEvaluator,
    ToolSelectionAccuracyEvaluator,
    TrajectoryEvaluator,
)
from strands_evals.types.trace import EvaluationLevel

from .contracts import LessonDraft
from .eval_scope import AgentToolsExtractor
from .node import AgentSpec, EvaluatorSpec, NodeConfig

SKILLS = Path(__file__).resolve().parents[2] / "knowledge" / "skills"

_FLOW = "Discover -> Implement -> Verify -> Learn"

_IMPLEMENT_STEERING = (
    "You guard tool calls for a coding agent working inside an isolated worktree. "
    "Proceed with reads and with writes to files inside the worktree. Guide the agent "
    "back on track if it tries to write outside the worktree or touch version-control "
    "internals. Interrupt any attempt to run destructive commands or exfiltrate data."
)


def _preamble(step: str) -> str:
    return (
        f"You are one agent in a 3-agent swarm working the {step} step of a coding-agent "
        f"workflow ({_FLOW}). Collaborate with your two teammates and converge on one "
        f"result. If you're stuck or a decision is outside your lane, hand off to a "
        f"teammate rather than guessing. When you hand off, pass a compact digest of what "
        f"you read and concluded (files, key lines, your findings) in the handoff context — "
        f"your teammate starts with no memory of your work, so anything you don't pass "
        f"they must re-read from scratch."
    )


def _rubric(name: str, rubric: str, threshold: float = 0.7) -> EvaluatorSpec:
    return EvaluatorSpec(
        name=name, evaluator_cls=OutputEvaluator, params={"rubric": rubric}, threshold=threshold
    )


def _judge(name: str, cls, threshold: float) -> EvaluatorSpec:
    return EvaluatorSpec(name=name, evaluator_cls=cls, threshold=threshold)


def _tool_judge(name: str, cls) -> EvaluatorSpec:
    """A per-tool-call judge: informational, and scoped to the agent's own tools.

    These rate each call individually ("was this call justified here?"), so they're
    diagnostics, not a verdict on the node — a few unjustified reads in a long
    investigation is normal. They run on every node that uses tools and their findings
    carry to the final node.
    """
    return EvaluatorSpec(
        name=name,
        evaluator_cls=cls,
        threshold=0.75,
        gating=False,
        trace_extractor=AgentToolsExtractor(EvaluationLevel.TOOL_LEVEL),
    )


def build_reference_nodes(
    *,
    worktree_tools: list,
    policy_tool,
    recall_tool=None,
    primed_lessons: str = "",
) -> list[NodeConfig]:
    lessons_block = (
        f"\n\nRelevant lessons from past runs:\n{primed_lessons}" if primed_lessons else ""
    )
    # Read-only context tools shared across nodes: policy lookup + recall of prior lessons.
    # Memory writes stay gated in code (junk-resistance) — there is no write tool.
    context_tools = [policy_tool] + ([recall_tool] if recall_tool is not None else [])
    read_tools = [*context_tools, *worktree_tools]

    discover = NodeConfig(
        name="discover",
        agents=[
            AgentSpec(
                name="scout",
                role="builder",
                system_prompt=(
                    _preamble("Discover") + " As the scout, read the target repo to locate "
                    "the code relevant to the ticket and propose a classification — 'bug', "
                    "'feature', or 'refuse' — with a short plan a coder could follow."
                    + lessons_block
                ),
                tools=list(read_tools),
            ),
            AgentSpec(
                name="analyst",
                role="reviewer",
                system_prompt=(
                    _preamble("Discover") + " As the analyst, verify the scout's read: check "
                    "the classification and that the plan cites code that actually exists. "
                    "Correct anything wrong, then hand to the challenger for the final call."
                ),
                tools=list(read_tools),
            ),
            AgentSpec(
                name="challenger",
                role="third",
                system_prompt=(
                    _preamble("Discover") + " As the challenger and final authority, stress-test "
                    "scope and safety: push for 'refuse' when the ticket is unsafe, out of scope, "
                    "or too underspecified. Commit to ONE bug/feature/refuse decision with the "
                    "plan and stop; only hand back if you have a specific, concrete defect."
                ),
                tools=list(read_tools),
            ),
        ],
        skill_paths=[SKILLS / "triage"],
        evaluators=[
            _judge("faithfulness", FaithfulnessEvaluator, 0.75),
            _judge("response_relevance", ResponseRelevanceEvaluator, 0.75),
            _tool_judge("tool_selection", ToolSelectionAccuracyEvaluator),
        ],
    )

    implement = NodeConfig(
        name="implement",
        agents=[
            AgentSpec(
                name="builder",
                role="builder",
                system_prompt=(
                    _preamble("Implement") + " As the builder, implement the plan by editing "
                    "files inside the worktree with the file tools. Make the smallest change "
                    "that resolves the ticket; add or update a test when it warrants one."
                ),
            ),
            AgentSpec(
                name="reviewer",
                role="reviewer",
                system_prompt=(
                    _preamble("Implement") + " As the reviewer, check the builder's change "
                    "against the plan and the repo's conventions. If it's wrong or risky, say "
                    "what to fix and hand back; when it's sound, hand to the breaker."
                ),
            ),
            AgentSpec(
                name="breaker",
                role="third",
                system_prompt=(
                    _preamble("Implement") + " As the breaker and final gate, try to break the "
                    "change: probe edge cases, regressions, and untested paths. Hand back only "
                    "with a specific, concrete defect; otherwise sign off with a short summary."
                ),
            ),
        ],
        steering_prompt=_IMPLEMENT_STEERING,
        shared_tools=[*worktree_tools, *context_tools],
        skill_paths=[SKILLS / "safe-change"],
        evaluators=[
            # The gate: goal attainment over the whole session (binary yes/no), so an
            # already-correct worktree after a retry still passes. Tool-call and trajectory
            # judges observe *how* the change was made — informative, never a veto.
            _judge("goal_success", GoalSuccessRateEvaluator, 1.0),
            _judge("faithfulness", FaithfulnessEvaluator, 0.75),
            _tool_judge("tool_params", ToolParameterAccuracyEvaluator),
            _tool_judge("tool_selection", ToolSelectionAccuracyEvaluator),
            EvaluatorSpec(
                name="trajectory",
                evaluator_cls=TrajectoryEvaluator,
                params={"rubric": (
                    "Judge the path taken to the change: did the agent read the code it "
                    "needed before editing, keep the edit scoped to the ticket, and avoid "
                    "redundant or irrelevant work?"
                )},
                threshold=0.7,
                gating=False,
            ),
        ],
        max_handoffs=16,
        max_iterations=16,
    )

    verify = NodeConfig(
        name="verify",
        agents=[
            AgentSpec(
                name="verifier",
                role="builder",
                system_prompt=(
                    _preamble("Verify") + " As the verifier, inspect the change and the tests "
                    "and report whether it looks correct and complete. The authoritative "
                    "test-gate is run by the platform; summarize evidence and flag gaps."
                ),
                tools=list(read_tools),
            ),
            AgentSpec(
                name="checker",
                role="reviewer",
                system_prompt=(
                    _preamble("Verify") + " As the checker, confirm the verifier's evidence "
                    "against the actual diff and tests — cite exit codes and specific hunks. "
                    "Correct any overclaim."
                ),
                tools=list(read_tools),
            ),
            AgentSpec(
                name="auditor",
                role="third",
                system_prompt=(
                    _preamble("Verify") + " As the auditor and final voice, give an unambiguous "
                    "correct / not-correct call grounded in the cited evidence, and stop."
                ),
                tools=list(read_tools),
            ),
        ],
        skill_paths=[SKILLS / "evidence-check"],
        evaluators=[
            _judge("faithfulness", FaithfulnessEvaluator, 0.75),
            _judge("coherence", CoherenceEvaluator, 0.5),
            _tool_judge("tool_selection", ToolSelectionAccuracyEvaluator),
        ],
    )

    learn = NodeConfig(
        name="learn",
        agents=[
            AgentSpec(
                name="drafter",
                role="builder",
                system_prompt=(
                    _preamble("Learn") + " As the drafter, given the eval results from every "
                    "stage, draft ONE durable lesson for next time: on success the pattern that "
                    "worked, on failure what went wrong and how to avoid it. Recall existing "
                    "lessons first so you don't repeat one already stored, and do not narrate "
                    "that recall — the rule is the whole answer."
                ),
                tools=list(context_tools),
            ),
            AgentSpec(
                name="refiner",
                role="reviewer",
                system_prompt=(
                    _preamble("Learn") + " As the refiner, tighten the draft into a single "
                    "actionable, generalizable rule — strip ticket-specific trivia, secrets, "
                    "and paths. Hand to the critic."
                ),
                tools=list(context_tools),
            ),
            AgentSpec(
                name="critic",
                role="third",
                system_prompt=(
                    _preamble("Learn") + " As the critic and final voice, ensure the output is "
                    "exactly ONE reusable lesson that isn't a near-duplicate of a recalled one. "
                    "Output only the lesson, and stop."
                ),
                tools=list(context_tools),
            ),
        ],
        skill_paths=[SKILLS / "lesson-writing"],
        # The node's answer is a schema, not prose. This swarm stops at its entry point in
        # practice, so the refiner and critic that were meant to strip framing never run;
        # declaring the shape gets it from whichever agent actually finishes.
        output_model=LessonDraft,
        evaluators=[
            _rubric(
                "lesson_shape",
                "The output is a JSON object with a `rule` field. Judge the rule only. Pass if "
                "it is exactly ONE durable, generalizable lesson phrased as an actionable rule "
                "— not zero, not many, not ticket-specific trivia — carries no preamble or "
                "commentary about the agent's own process, and contains no secrets, absolute "
                "paths, or run-specific IDs.",
                threshold=0.5,
            ),
            _judge("conciseness", ConcisenessEvaluator, 0.5),
        ],
    )

    return [discover, implement, verify, learn]
