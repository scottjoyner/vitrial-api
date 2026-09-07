from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vitrial:vitrial@localhost:5432/vitrial"
    database_null_pool: bool = False
    service_version: str = "0.1.0"
    api_version: str = "v1"

    evidence_storage_provider: Literal["local", "s3"] = "local"
    evidence_root: Path = Path(".evidence")
    evidence_gc_grace_seconds: int = 86400

    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "vitrial-evidence"
    s3_region: str = "us-east-1"
    s3_part_size_bytes: int = 5 * 1024 * 1024

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
