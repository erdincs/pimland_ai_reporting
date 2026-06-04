"""Centralised, 12-factor configuration.

Every setting is read from the environment (or `.env` locally). Nothing else in
the codebase should read `os.environ` directly — import `settings` from here so
configuration stays typed, validated and discoverable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List, Literal, Optional

from pydantic import Field, PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_name: str = "Pimland AI Reporting"
    log_level: str = "INFO"

    # --- API ---
    api_v1_prefix: str = "/api/v1"
    cors_origins: List[str] = Field(default_factory=list)

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "pimland"
    postgres_password: str = "change_me"
    postgres_db: str = "pimland_reporting"
    postgres_readonly_user: Optional[str] = None
    postgres_readonly_password: Optional[str] = None

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    cache_ttl_seconds: int = 3600

    # --- LLM (AWS Bedrock) ---
    # Short model ID or full inference-profile ARN.
    # ARN format: arn:aws:bedrock:<region>:<account>:inference-profile/<id>
    bedrock_model_id: str = "anthropic.claude-sonnet-4-5"
    anthropic_max_tokens: int = 4096
    llm_timeout_seconds: int = 120

    # --- Pimland MCP-1 credentials ---
    pimland_token_url: str = "https://ids.pimland.com/connect/token"
    pimland_client_id: Optional[str] = None
    pimland_client_secret: Optional[str] = None
    pimland_scope: Optional[str] = None
    pimland_username: Optional[str] = None
    pimland_password: Optional[str] = None

    # --- AWS (Bedrock + S3) ---
    aws_region: str = "eu-central-1"
    s3_bucket: str = "pimland-reporting-artifacts"
    # Explicit credentials for local dev. In production leave empty — the
    # EC2/ECS instance role is used automatically via the boto3 chain.
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bedrock_region(self) -> str:
        """Extract region from model ARN, or fall back to aws_region.

        ARN example:
          arn:aws:bedrock:eu-north-1:123456789:inference-profile/eu.anthropic...
          parts[3] = 'eu-north-1'
        """
        if self.bedrock_model_id.startswith("arn:aws:bedrock:"):
            parts = self.bedrock_model_id.split(":")
            if len(parts) >= 4:
                return parts[3]
        return self.aws_region

    # --- SQL guard ---
    sql_max_limit: int = 10_000
    sql_statement_timeout_ms: int = 15_000

    # ── Derived connection strings ──────────────────────────────────────────
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async DSN (asyncpg) used by the application at runtime."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Sync DSN (psycopg2) for Alembic migrations and Excel ingestion."""
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg2",
                username=self.postgres_user,
                password=self.postgres_password,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def readonly_database_url(self) -> str:
        """Async DSN for the least-privilege role that runs generated SQL.

        Falls back to the main credentials if no read-only role is configured,
        so local dev works without extra setup — but production MUST set one.
        """
        user = self.postgres_readonly_user or self.postgres_user
        pwd = self.postgres_readonly_password or self.postgres_password
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=user,
                password=pwd,
                host=self.postgres_host,
                port=self.postgres_port,
                path=self.postgres_db,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return str(
            RedisDsn.build(
                scheme="redis",
                password=self.redis_password or None,
                host=self.redis_host,
                port=self.redis_port,
                path=str(self.redis_db),
            )
        )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Use this everywhere (incl. as a FastAPI dependency)."""
    return Settings()


settings = get_settings()
