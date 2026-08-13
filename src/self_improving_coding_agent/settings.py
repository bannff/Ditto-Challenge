"""Env-driven configuration. The one typed source of config and model construction.

No real secrets live here — Bedrock access comes from the AWS profile/creds, not from
values in this file. If a true secret is ever added, wrap it in pydantic.SecretStr.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from strands.models import BedrockModel

# Adaptive retries absorb Bedrock throttling when graph nodes run concurrently;
# a wider pool avoids connection starvation across a swarm's agents.
_BOTO_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    max_pool_connections=50,
    read_timeout=120,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    bedrock_model_id: str
    bedrock_embed_model_id: str
    aws_region: str
    bedrock_reviewer_model_id: str | None = None
    bedrock_embed_dims: int = 1024
    aws_profile: str = "default"
    data_dir: Path = Field(default=Path(".data"))

    @property
    def reviewer_model_id(self) -> str:
        return self.bedrock_reviewer_model_id or self.bedrock_model_id

    @property
    def ledger_db(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def mem0_dir(self) -> Path:
        return self.data_dir / "mem0"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    def ensure_dirs(self) -> None:
        for p in (self.chroma_dir, self.mem0_dir, self.sessions_dir, self.worktrees_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from env/.env


@lru_cache(maxsize=1)
def _session() -> boto3.Session:
    s = get_settings()
    return boto3.Session(profile_name=s.aws_profile, region_name=s.aws_region)


def aws_credentials() -> dict[str, str]:
    """Frozen creds for libraries that build their own boto3 client (e.g. Mem0), which
    can't take our session or an aws_profile kwarg. Refresh creds if a run outlives them."""
    s = get_settings()
    frozen = _session().get_credentials().get_frozen_credentials()
    creds = {
        "aws_region": s.aws_region,
        "aws_access_key_id": frozen.access_key,
        "aws_secret_access_key": frozen.secret_key,
    }
    if frozen.token:
        creds["aws_session_token"] = frozen.token
    return creds


def build_model(
    model_id: str | None = None, *, temperature: float = 0.2, max_tokens: int = 4096
) -> BedrockModel:
    s = get_settings()
    return BedrockModel(
        model_id=model_id or s.bedrock_model_id,
        boto_session=_session(),
        boto_client_config=_BOTO_CONFIG,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def build_reviewer_model(*, temperature: float = 0.0, max_tokens: int = 2048) -> BedrockModel:
    return build_model(
        get_settings().reviewer_model_id, temperature=temperature, max_tokens=max_tokens
    )
