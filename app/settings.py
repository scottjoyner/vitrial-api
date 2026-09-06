from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vitrial:vitrial@localhost:5432/vitrial"
    database_null_pool: bool = False
    service_version: str = "0.1.0"
    api_version: str = "v1"
    evidence_root: Path = Path(".evidence")
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
