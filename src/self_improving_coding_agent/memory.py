"""Durable lesson memory: Mem0 with Bedrock LLM/embedder and a local FAISS store.

The wrapper is the single source of truth. The stock strands_tools.mem0_memory tool can't
share our store (it hardcodes a /tmp path and writes unscrubbed), so the Learn agent gets
a thin tool bound to this same instance instead.
"""

from __future__ import annotations

from pathlib import Path

from mem0 import Memory
from strands import tool

from .contracts import Lesson
from .scrub import scrub_text
from .settings import aws_credentials, get_settings

USER_ID = "autodev"


def _config(store_dir: Path) -> dict:
    s = get_settings()
    creds = aws_credentials()
    return {
        "llm": {
            "provider": "aws_bedrock",
            "config": {"model": s.bedrock_model_id, "temperature": 0.1, "max_tokens": 512, **creds},
        },
        "embedder": {
            "provider": "aws_bedrock",
            "config": {
                "model": s.bedrock_embed_model_id,
                "embedding_dims": s.bedrock_embed_dims,
                **creds,
            },
        },
        "vector_store": {
            "provider": "faiss",
            "config": {
                "collection_name": "lessons",
                "path": str(store_dir),
                "embedding_model_dims": s.bedrock_embed_dims,
                "distance_strategy": "cosine",
            },
        },
        "history_db_path": str(store_dir / "history.db"),
    }


class LessonMemory:
    def __init__(self, storage_dir: Path | None = None) -> None:
        store_dir = storage_dir or get_settings().mem0_dir
        store_dir.mkdir(parents=True, exist_ok=True)
        self._m = Memory.from_config(_config(store_dir))

    def store(self, lesson: Lesson) -> None:
        # Store both outcomes, verbatim (the Learn node already distilled the text) and
        # scrubbed. infer=False keeps it deterministic and skips an extra Bedrock call.
        content = scrub_text(lesson.content)
        # Dedup: a re-run of the same ticket produces a near-identical lesson; don't let
        # memory accumulate duplicates (junk-resistance).
        existing = self._m.search(content, filters={"user_id": USER_ID}, limit=1)
        hits = existing.get("results", []) if isinstance(existing, dict) else (existing or [])
        if hits and (hits[0].get("score") or 0) >= 0.95:
            return
        self._m.add(
            content,
            user_id=USER_ID,
            infer=False,
            metadata={
                "outcome": str(lesson.outcome),
                "ticket_id": lesson.ticket_id,
                "tags": lesson.tags,
                "schema_version": lesson.schema_version,
                "created_at": lesson.created_at.isoformat(),
            },
        )

    def retrieve(self, query: str, limit: int = 3) -> list[str]:
        res = self._m.search(query, filters={"user_id": USER_ID}, limit=limit)
        hits = res.get("results", []) if isinstance(res, dict) else res
        return [h.get("memory", "") for h in hits if h.get("memory")]


def make_memory_tools(memory: LessonMemory) -> list:
    """Tools for the Learn node's agent, bound to the shared LessonMemory."""

    @tool
    def recall_lessons(query: str) -> str:
        """Search durable memory for lessons from past runs relevant to the query."""
        hits = memory.retrieve(query)
        return "\n".join(f"- {h}" for h in hits) if hits else "No prior lessons found."

    return [recall_lessons]
