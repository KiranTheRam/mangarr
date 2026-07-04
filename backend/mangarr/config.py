import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Process-level configuration (env vars / .env). Runtime-editable
    settings (qBittorrent creds, naming, etc.) live in the Settings table."""

    model_config = SettingsConfigDict(env_prefix="MANGARR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 6996
    log_level: str = "INFO"
    # When unset, an API key is generated on first run and stored in data_dir/api_key
    api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mangarr.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        key_file = self.data_dir / "api_key"
        if key_file.exists():
            return key_file.read_text().strip()
        key = secrets.token_hex(16)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key)
        return key


config = AppConfig()
