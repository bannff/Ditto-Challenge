from strands_evals.evaluators import OutputEvaluator

from self_improving_coding_agent.node import AgentSpec, EvaluatorSpec, NodeConfig


def test_node_config_defaults_and_shape():
    node = NodeConfig(
        name="discover",
        agents=[AgentSpec(name="scout", system_prompt="find the code")],
        evaluators=[
            EvaluatorSpec(
                name="output",
                evaluator_cls=OutputEvaluator,
                params={"rubric": "Pass if the plan is grounded in the repo."},
                threshold=0.7,
            )
        ],
    )
    assert node.agents[0].role == "builder"
    assert node.evaluators[0].evaluator_cls is OutputEvaluator
    assert node.max_redos == 2
    assert node.max_handoffs == 12 and node.execution_timeout == 600.0


def test_evaluator_spec_is_pure_declaration():
    spec = EvaluatorSpec(name="c", evaluator_cls=OutputEvaluator, params={"rubric": "x"})
    # It declares; it does not construct. No model is minted here.
    assert isinstance(spec.evaluator_cls, type)
    assert spec.params == {"rubric": "x"}
