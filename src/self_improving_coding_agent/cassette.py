"""Recorded model I/O, so a run's decisions can be re-executed offline.

A cassette holds the raw `StreamEvent` sequence of every model call, which makes replay
byte-faithful: the SDK re-derives the assembled message, usage, metrics, and every typed
stream event from those chunks, so nothing has to be synthesised.

## What is actually on disk, and why it is still bounded to fixture repos

Only **responses**. A request is stored as a digest, never as text, because replay only needs
to *recognise* a request, not reproduce it. So the prompt — with the repository source it
quotes, the ticket text, the tool results, and the primed lessons from memory — never reaches
the file at all. That is a much narrower exposure than "record the model conversation," and
worth stating precisely rather than over-warning.

What remains is still sensitive: a response carries the model's own words *and* its tool-use
arguments, so the body of every file the agent writes is in here, including anything it copied
out of a file it read. `scrub_text` cannot police that — it catches fixed-prefix tokens, PEM
blocks, and `key=value` where the key names a secret, so arbitrary repo source walks straight
through — and scrubbing would break replay anyway: rewrite a recorded response and the model
no longer replays what it actually said. Fidelity and redaction cannot both hold, so this file
carries no "scrubbed" label, which is the label that gets such a file committed.

Recording is therefore refused unless the target repo lives under an explicitly configured
fixture root (`cassette_fixture_root`). Unset means off. That is the whole capability, bounded
to where it is correct, rather than a redaction that cannot work.

## Why a replayed run can never ship

Replay drives tool calls from recorded model output. Recorded output is not evidence, so a
replayed run re-runs the acceptance gate for real or reports nothing — and it is never
allowed to teach memory. `ReplayModel` fails loud on a cassette miss rather than falling
through to the live model: a silent fallback would mean a "replay" that quietly cost money
and proved nothing.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, AsyncIterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from .contracts import RUN_ID_RE
from .model_record import digest_request, digest_system

T = TypeVar("T", bound=BaseModel)

HEADER = "UNREDACTED MODEL I/O — repository source and prompts, recorded from a fixture repo"

# The bytes-valued corner of StreamEvent: contentBlockDelta.delta.reasoningContent
# .redactedContent is raw bytes, which JSON cannot hold.
_BYTES_MARKER = "__b64__"


class CassetteError(RuntimeError):
    """Recording or replay was refused, or a cassette does not match the run."""


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if _BYTES_MARKER in value and len(value) == 1:
            return base64.b64decode(value[_BYTES_MARKER])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def recording_allowed(repo: Path, fixture_root: Path | None) -> str | None:
    """None if this repo may be recorded, else the reason it may not."""
    if fixture_root is None:
        return (
            "cassette recording is off: set cassette_fixture_root to a fixture directory. "
            "Cassettes hold unredacted prompts, so recording real repositories is refused."
        )
    root = fixture_root.resolve()
    target = repo.resolve()
    if target != root and root not in target.parents:
        return (
            f"refusing to record {target}: it is outside the fixture root {root}. "
            "A cassette holds unredacted repository source."
        )
    return None


class Cassette:
    """One run's recorded model calls, on disk as JSON Lines.

    Entries are keyed by the digest of the request plus an occurrence counter, so replay is
    content-addressed. A miss therefore means the replayed run diverged from the recording —
    the signal worth having — rather than silently serving the wrong response the way a bare
    ordinal would.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mode: str | None = None  # "record" or "replay", set by open_for_record / load
        self._counts: dict[str, int] = {}
        self._entries: dict[tuple[str, int], dict[str, Any]] = {}

    @classmethod
    def for_run(cls, run_id: str, cassettes_dir: Path) -> Cassette:
        # run_id reaches here from argv; it becomes a path component, so it is validated
        # before it can traverse (the same guard Worktree.create applies).
        if not RUN_ID_RE.fullmatch(run_id):
            raise CassetteError(f"invalid run id: {run_id!r}")
        base = cassettes_dir.resolve()
        path = (base / f"{run_id}.jsonl").resolve()
        if path.parent != base:
            raise CassetteError(f"cassette path escapes {base}: {run_id!r}")
        return cls(path)

    # ---- record ---------------------------------------------------------------

    def open_for_record(self, repo: Path, fixture_root: Path | None) -> Cassette:
        refusal = recording_allowed(repo, fixture_root)
        if refusal is not None:
            raise CassetteError(refusal)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"header": HEADER, "repo": str(repo.resolve())}) + "\n")
        # Owner-only: the contents are unredacted by design.
        self.path.chmod(0o600)
        self.mode = "record"
        return self

    def write(self, key: str, events: list[StreamEvent]) -> None:
        occurrence = self._counts.get(key, 0)
        self._counts[key] = occurrence + 1
        with self.path.open("a") as handle:
            handle.write(
                json.dumps(
                    {"key": key, "occurrence": occurrence, "events": _encode(events)}
                )
                + "\n"
            )

    # ---- replay ---------------------------------------------------------------

    def load(self, repo: Path | None = None) -> Cassette:
        if not self.path.exists():
            raise CassetteError(f"no cassette at {self.path}")
        recorded_repo: str | None = None
        for line in self._lines():
            entry = json.loads(line)
            if "header" in entry:
                recorded_repo = entry.get("repo")
                continue
            self._entries[(entry["key"], entry["occurrence"])] = entry
        if not self._entries:
            raise CassetteError(f"cassette at {self.path} has no recorded calls")
        # A cassette recorded against repo A must not drive writes into repo B: the recorded
        # decisions reference paths and content that only make sense in their own repo.
        if repo is not None and recorded_repo and str(repo.resolve()) != recorded_repo:
            raise CassetteError(
                f"cassette was recorded against {recorded_repo}, not {repo.resolve()}"
            )
        self.mode = "replay"
        return self

    def _lines(self) -> Iterator[str]:
        with self.path.open() as handle:
            yield from handle

    def take(self, key: str) -> list[StreamEvent]:
        occurrence = self._counts.get(key, 0)
        entry = self._entries.get((key, occurrence))
        if entry is None:
            raise CassetteError(
                f"no recorded response for request {key[:12]} (occurrence {occurrence}). "
                "The replayed run diverged from the recording, so replay stops here rather "
                "than calling the live model."
            )
        self._counts[key] = occurrence + 1
        return _decode(entry["events"])

    def __len__(self) -> int:
        return len(self._entries)


class RecordingCassetteModel(Model):
    """Wraps a model, tees every call's raw events into a cassette, yields them through."""

    def __init__(self, inner: Model, cassette: Cassette) -> None:
        self._inner = inner
        self._cassette = cassette

    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self._inner.get_config()

    @property
    def stateful(self) -> bool:
        return self._inner.stateful

    @property
    def context_window_limit(self) -> int | None:
        return self._inner.context_window_limit

    async def count_tokens(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: Any = None,
    ) -> int:
        return await self._inner.count_tokens(
            messages, tool_specs, system_prompt, system_prompt_content
        )

    def estimate_utilization(self, input_tokens: int) -> float:
        return self._inner.estimate_utilization(input_tokens)

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        key = cassette_key(messages, tool_specs, system_prompt)
        captured: list[StreamEvent] = []
        try:
            async for event in self._inner.stream(
                messages, tool_specs, system_prompt, **kwargs
            ):
                captured.append(event)
                yield event
        finally:
            self._cassette.write(key, captured)

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        # Not recorded: the modern structured-output path is a forced tool call over
        # stream(), so it is captured there. Only the deprecated Model.structured_output
        # reaches here, and this repo never calls it.
        async for event in self._inner.structured_output(
            output_model, prompt, system_prompt, **kwargs
        ):
            yield event


class ReplayModel(Model):
    """Serves recorded events. Never reaches the network; never falls back to a live model."""

    def __init__(self, cassette: Cassette, *, model_id: str = "cassette-replay") -> None:
        self._cassette = cassette
        self._model_id = model_id

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": self._model_id}

    async def count_tokens(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: Any = None,
    ) -> int:
        return 0

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        for event in self._cassette.take(cassette_key(messages, tool_specs, system_prompt)):
            yield event

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        raise CassetteError(
            "Model.structured_output is not replayable: it bypasses hooks and middleware, "
            "and this project uses the structured_output_model= path instead, which replays "
            "through stream()."
        )
        yield {}  # pragma: no cover - unreachable, satisfies the generator signature


def model_wrapper(chain_wrapper: Any, cassette: Cassette | None) -> Any:
    """Build the per-agent model wrapper for this run's mode.

    The chain recorder is outermost in every mode, so a replayed run leaves its own
    MODEL_CALL digests — which is precisely what makes the recorded and replayed chains
    comparable, and how divergence becomes visible.
    """

    def wrap(inner: Model, node: str, agent: str) -> Model:
        model = inner
        if cassette is not None and cassette.mode == "replay":
            model = ReplayModel(cassette)
        elif cassette is not None and cassette.mode == "record":
            model = RecordingCassetteModel(model, cassette)
        return chain_wrapper(model, node, agent)

    return wrap


def cassette_key(
    messages: list[Message],
    tool_specs: list[ToolSpec] | None,
    system_prompt: str | None,
) -> str:
    """Content address for one model call.

    Includes the system prompt, unlike the chain's `request_hash`, which deliberately hashes
    it apart so growing memory doesn't read as divergence. Replay needs the opposite: an
    exact match on everything the model was actually given.
    """
    return digest_request(messages, tool_specs) + digest_system(system_prompt)[:16]
