"""Runtime configuration, read from environment variables.

Everything has a working default so the server runs with zero configuration
inside docker compose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    embedding_model: str
    # Must match the vector(N) column in schema.sql; checked against the model at startup.
    embedding_dim: int
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://rag:rag@localhost:5433/rag"
            ),
            embedding_model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            embedding_dim=int(os.environ.get("EMBEDDING_DIM", "384")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
