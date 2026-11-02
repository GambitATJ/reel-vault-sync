from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    GITHUB_PERSONAL_ACCESS_TOKEN: str = ""
    GITHUB_VAULT_REPO: str = "owner/vault-repo-name"
    GITHUB_VAULT_BRANCH: str = "main"
    VAULT_ROOT_DIR: str = "Inbox/Reels"
    API_AUTH_TOKEN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
