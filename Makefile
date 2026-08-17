# Thin wrappers over the uv commands in README.md — no build logic lives here.
# `setup` and `demo` are the two commands the brief asks a reviewer to need.
.PHONY: setup check test demo demo-selfimprove redteam chaos

setup:                ## Install dependencies and create .env from the example
	uv sync
	@test -f .env || cp .env.example .env
	@echo
	@echo "Set your Bedrock model IDs and AWS_REGION/AWS_PROFILE in .env, then: make demo"

check:                ## Lint and type-check
	uv run ruff check src tests hardening scripts
	uv run pyright

test:                 ## Unit tests + the keyless adversarial suite (~3 min, no credentials)
	uv run pytest

# app2 on purpose: it is the harder target, so the swarm actually collaborates instead of
# one-shotting. Deliberately not a chain of every demo either — five back-to-back runs is a
# good way to get throttled, and each one is more legible watched on its own.
demo:                 ## THE run command: bug + feature + refusal on the harder app (needs Bedrock)
	uv run python scripts/demo.py --app app2 --out demos/generated
	@echo
	@echo "The rest, one command each:"
	@echo "  make demo-selfimprove  empty vs primed memory (the before/after)"
	@echo "  make demo-app1         the same three outcomes on the simpler app"
	@echo "  make demo-ledger       rollback and tamper-evidence, no Bedrock needed"

demo-selfimprove:     ## Self-improvement: same scenario with empty vs primed memory
	uv run python scripts/demo_selfimprove.py --out demos/self-improvement

demo-app1:            ## The three outcomes on the simpler target app
	uv run python scripts/demo.py --app app1 --out demos/generated

demo-ledger:          ## Rollback, recovery and tamper-evidence, offline (no Bedrock)
	uv run python scripts/demo_ledger.py

redteam:              ## Adaptive LLM attacker vs the Implement swarm (needs Bedrock)
	uv run python hardening/redteam_run.py

chaos:                ## Tool failures injected mid-change (needs Bedrock)
	uv run python hardening/chaos_run.py
