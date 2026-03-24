from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXY_POOL_", case_sensitive=False)

    data_dir: Path = Field(default=Path("/app/data"))
    state_file: str = "state.json"
    singbox_config_file: str = "sing-box.json"
    singbox_binary: str = "/usr/local/bin/sing-box"
    bind_host: str = "0.0.0.0"
    proxy_public_host: str = "proxy-pool"
    api_host: str = "0.0.0.0"
    api_port: int = 9080
    base_port: int = 20001
    pool_size: int = 20
    strategy: str = "random"
    lease_ttl_secs: int = 600
    cleanup_interval_secs: int = 30
    healthcheck_url: str = "https://www.gstatic.com/generate_204"
    healthcheck_timeout: int = 10
    healthcheck_interval_secs: int = 300
    healthcheck_concurrency: int = 8
    allocation_stale_after_secs: int = 300
    log_level: str = "info"

    @property
    def state_path(self) -> Path:
        return self.data_dir / self.state_file

    @property
    def singbox_config_path(self) -> Path:
        return self.data_dir / self.singbox_config_file


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
