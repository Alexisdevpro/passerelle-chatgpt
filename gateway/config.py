from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GW_", env_file=".env", extra="ignore")
    data_dir: Path = Path(".local")
    provider: Literal["chatgpt_oauth", "ollama"] = "chatgpt_oauth"
    host: str = "127.0.0.1"
    port: int = 8787
    ollama_url: str = "http://127.0.0.1:11434"
    request_timeout: float = 180
    max_body_bytes: int = 16_777_216
    body_read_timeout: float = 30
    max_output_bytes: int = 4_194_304
    max_concurrency: int = 2
    key_rpm: int = 30
    ip_rpm: int = 60
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    allowed_origins: list[str] = []
    trusted_proxy_cidrs: list[str] = []
    catalog_ttl_seconds: float = 30

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_proxy_networks(cls, value):
        import ipaddress

        for item in value:
            network = ipaddress.ip_network(item, strict=True)
            if network.prefixlen == 0:
                raise ValueError("Do not trust all proxy addresses")
        return value

    @field_validator("ollama_url")
    @classmethod
    def local_ollama(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama must use a loopback HTTP URL; use a local forward for a remote server.")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Ollama URL must contain only scheme, loopback host and port.")
        return value.rstrip("/")
