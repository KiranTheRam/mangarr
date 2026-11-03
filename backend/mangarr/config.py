import secrets
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Process-level configuration (env vars / .env). Runtime-editable
    settings (qBittorrent creds, naming, etc.) live in the Settings table."""

    model_config = SettingsConfigDict(env_prefix="MANGARR_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 6996
    log_level: str = "INFO"
    # When unset, an API key is generated on first run and stored in data_dir/api_key
    api_key: str | None = None
    # Opt-in browser HTTP Basic login. API clients continue using X-Api-Key.
    auth_username: str = ""
    auth_password: SecretStr = SecretStr("")
    # Additional exact DNS names (JSON array); IP literals and localhost work
    # without configuration. Never infer trusted hosts from forwarded headers.
    allowed_hosts: list[str] = []
    # SQLite write-ahead log. Needs local storage for data_dir; set to false
    # if /config lives on NFS/SMB, where WAL's shared memory file is unreliable.
    sqlite_wal: bool = True

    @model_validator(mode="after")
    def validate_security(self):
        if bool(self.auth_username) != bool(self.auth_password.get_secret_value()):
            raise ValueError("Set both MANGARR_AUTH_USERNAME and MANGARR_AUTH_PASSWORD, or neither")
        if ":" in self.auth_username:
            raise ValueError("MANGARR_AUTH_USERNAME cannot contain a colon")
        for host in self.allowed_hosts:
            if not host or any(c in host for c in "/:*?@#[]\\") or host != host.strip():
                raise ValueError("MANGARR_ALLOWED_HOSTS must contain exact DNS names without ports or wildcards")
        return self

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
