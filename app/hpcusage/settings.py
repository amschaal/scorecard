import json
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a .env file locally)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://hpcusage:hpcusage@localhost:5432/hpcusage"
    app_base_url: str = "http://localhost:8000"
    session_secret: str = "dev-only-change-me"
    session_max_age_s: int = 12 * 3600

    # Auth: "cas" in production, "dev" auto-logs in dev_user (CAS cannot redirect to localhost).
    auth_mode: Literal["cas", "dev"] = "dev"
    dev_user: str = "devuser"
    cas_base: str = "https://cas.ucdavis.edu/cas"
    cas_version: int = 3  # python-cas protocol version: 1, 2 or 3 (3 = /p3/serviceValidate, returns attributes)
    # Comma-separated usernames; empty = every authenticated CAS user is allowed.
    allowed_users: str = ""

    # JSON: {"hive": "token", "farm": "token"}. Token identifies the cluster that may post.
    collector_tokens: str = "{}"
    # Optional JSON: {"hive": {"display_name": "Hive", "timezone": "America/Los_Angeles"}}
    clusters: str = "{}"

    job_retention_days: int = 400
    node_snapshot_retention_days: int = 730
    max_ingest_bytes: int = 200 * 1024 * 1024
    upsert_batch_size: int = 5000

    log_level: str = "info"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Accept plain postgresql:// (what RDS / pg tooling hand out) and force the psycopg3 driver.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def collector_token_map(self) -> dict[str, str]:
        return json.loads(self.collector_tokens or "{}")

    @property
    def cluster_config(self) -> dict[str, dict]:
        return json.loads(self.clusters or "{}")

    @property
    def allowed_user_set(self) -> set[str]:
        return {u.strip() for u in self.allowed_users.split(",") if u.strip()}

    @property
    def is_https(self) -> bool:
        return self.app_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
