# Thin wrappers over the uv commands in README.md — no build logic lives here.
# `setup` and `demo` are the two commands the brief asks a reviewer to need.
.PHONY: setup check test demo demo-selfimprove redteam chaos

setup:                ## Install dependencies and create .env from the example
	uv sync
	@test -f .env || cp .env.example .env
	@echo
	@echo "Set your Bedrock model IDs and AWS_REGION/AWS_PROFILE in .env, then: make demo"

check:                ## Lint and type-check
	uv run ruff check src tests breach scripts
	uv run pyright

test:                 ## Unit tests + the keyless adversarial suite (~3 min, no credentials)
	uv run pytest

demo:                 ## Resolve a bug, resolve a feature, refuse a ticket (needs Bedrock)
	uv run python scripts/demo.py --out demos/generated

demo-selfimprove:     ## The before/after memory comparison (needs Bedrock)
	uv run python scripts/demo_selfimprove.py --out demos/self-improvement

redteam:              ## Adaptive LLM attacker vs the Implement swarm (needs Bedrock)
	uv run python breach/redteam_run.py

chaos:                ## Tool failures injected mid-change (needs Bedrock)
	uv run python breach/chaos_run.py
