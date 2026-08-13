"""Policy knowledge base: local chromadb collection with similarity search."""

from __future__ import annotations

from pathlib import Path

import chromadb
from strands import tool

DEFAULT_POLICY_DOC = (
    Path(__file__).resolve().parents[2] / "knowledge" / "policies" / "security-best-practices.md"
)
COLLECTION = "policy"


def _chunks(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*").strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


class PolicyKB:
    def __init__(self, persist_dir: Path | str):
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._col = self._client.get_or_create_collection(COLLECTION)

    def seed(self, doc_path: Path | None = None) -> int:
        if self._col.count() == 0:
            chunks = _chunks((doc_path or DEFAULT_POLICY_DOC).read_text())
            self._col.add(ids=[f"p{i}" for i in range(len(chunks))], documents=chunks)
        return self._col.count()

    def query(self, text: str, n: int = 3) -> list[str]:
        count = self._col.count()
        if count == 0:
            return []
        res = self._col.query(query_texts=[text], n_results=min(n, count))
        return res["documents"][0] if res["documents"] else []

    def add_policy(self, text: str) -> None:
        self._col.add(ids=[f"p{self._col.count()}"], documents=[text])


def make_query_policy_tool(kb: PolicyKB):
    @tool
    def query_policy(query: str) -> str:
        """Search the security/policy knowledge base for rules relevant to the query."""
        hits = kb.query(query)
        return "\n".join(f"- {h}" for h in hits) if hits else "No relevant policy found."

    return query_policy
