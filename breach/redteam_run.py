"""Red-team the implement swarm with strands_evals' RedTeamExperiment.

Uses the SDK as documented for a multi-agent target: pass the `Swarm` as `agent` and let
`StrandsMultiAgentSession` drive it (snapshot/restore per attempt), with
`AttackSuccessEvaluator` judging the transcript + tool-use trace.

Two things we add on top, both deliberate:
  * every model (attacker, judge) is our env-configured Bedrock model — the SDK defaults to
    a hardcoded model in another region;
  * after the attacks we inspect the worktree ourselves. A judge saying "refused" is not
    evidence the jail held; the filesystem is.

Usage:
  uv run python breach/redteam_run.py            # 1 strategy x 4 cases, 3 turns each
  uv run python breach/redteam_run.py --turns 5 --strategy crescendo
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from strands_evals.experimental.redteam import (
    AttackSuccessEvaluator,
    CrescendoStrategy,
    GoatStrategy,
    RedTeamExperiment,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cases import CASES  # noqa: E402
from targets import build_target, jail_held, jail_report  # noqa: E402

from self_improving_coding_agent.settings import build_model  # noqa: E402
from self_improving_coding_agent.telemetry import setup_telemetry  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "scratch"


def _strategy(name: str, turns: int, model):
    if name == "goat":
        return GoatStrategy(max_turns=turns, model=model)
    return CrescendoStrategy(max_turns=turns, model=model)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Red-team the implement swarm.")
    p.add_argument("--turns", type=int, default=3,
                   help="cap per attack (each turn is a real swarm run)")
    p.add_argument("--strategy", default="crescendo", choices=["crescendo", "goat"])
    p.add_argument("--case", help="run only the case with this name")
    args = p.parse_args(argv)

    setup_telemetry(console=False)
    # One model for attacker + judge, from our env config (never the SDK's default).
    model = build_model(temperature=0.3)
    cases = [c for c in CASES if not args.case or c.name == args.case]

    target = build_target("implement")
    print(f"target worktree: {target.worktree.root}")
    print(f"canary outside jail: {target.canary}")
    print(f"cases: {[c.name for c in cases]} | strategy={args.strategy} | turns<={args.turns}\n")

    experiment = RedTeamExperiment(
        cases=cases,
        agent=target.swarm,
        attack_strategies=[_strategy(args.strategy, args.turns, model)],
        evaluators=[AttackSuccessEvaluator(model=model)],
        model=model,
    )
    report = experiment.run_evaluations()
    report.display(verbose=True)

    # The authoritative check: did anything actually cross the boundary?
    jail = jail_report(target)
    held = jail_held(jail)
    print("\n=== jail inspection (authoritative) ===")
    print(json.dumps(jail, indent=2))
    print(f"\nJAIL HELD: {held}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"breach_redteam_{stamp}.json"
    out.write_text(json.dumps({
        "strategy": args.strategy,
        "turns_cap": args.turns,
        "attacks": [
            {
                "case": r.case_name,
                "risk_category": r.risk_category,
                "strategy": r.strategy,
                "severity": r.severity,
                "turns_used": r.turns_used,
                "breached": not r.passed,
                "score": r.score,
                "reason": r.reason,
                "conversation": r.conversation,
            }
            for r in report.attack_results
        ],
        "jail": jail,
        "jail_held": held,
    }, indent=2, default=str))
    print(f"saved: {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
