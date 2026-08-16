"""Turn node data (AgentSpec + node-level skills/steering) into real Strands Agents.

Plugins attach to the Agent, never the Swarm. Skills and steering are node-scoped: every
agent in a node shares that node's skill set and steering guidance (SPEC 2c). Models are
built once by the caller and passed in — never minted here — so nothing constructs a
boto client on a running node.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from strands import Agent
from strands.models.model import Model
from strands.vended_plugins.skills import AgentSkills
from strands.vended_plugins.steering import LLMSteeringHandler

from .node import AgentSpec, NodeConfig


def build_agent(
    spec: AgentSpec,
    *,
    model: Model,
    skill_paths: list[Path] | None = None,
    steering_prompt: str | None = None,
    shared_tools: list | None = None,
    extra_plugins: list | None = None,
    hooks: list | None = None,
) -> Agent:
    plugins = []
    if skill_paths:
        plugins.append(AgentSkills(skills=list(skill_paths)))
    if steering_prompt:
        plugins.append(LLMSteeringHandler(system_prompt=steering_prompt))
    plugins.extend(extra_plugins or [])
    return Agent(
        name=spec.name,
        model=model,
        system_prompt=spec.system_prompt,
        tools=[*spec.tools, *(shared_tools or [])],
        plugins=plugins,
        hooks=list(hooks or []),
    )


def _model_for(node: NodeConfig, spec: AgentSpec, models: Mapping[str, Model]) -> Model:
    """The role's shared model, wrapped per agent when the node asks for it."""
    model = models[spec.role]
    if node.model_wrapper is None:
        return model
    return node.model_wrapper(model, node.name, spec.name)


def build_node_agents(node: NodeConfig, models: Mapping[str, Model]) -> list[Agent]:
    """One fresh Agent per AgentSpec, model selected by role, node plugins shared."""
    return [
        build_agent(
            spec,
            model=_model_for(node, spec, models),
            skill_paths=node.skill_paths,
            steering_prompt=node.steering_prompt,
            shared_tools=node.shared_tools,
            extra_plugins=node.extra_plugins,
            hooks=node.hooks,
        )
        for spec in node.agents
    ]
