from pathlib import Path
from typing import Dict

import yaml
from pydantic import BaseModel, ValidationError,Field


class ProviderConfig(BaseModel):
    name: str
    protocol: str  # "http" or "websocket"
    rpc_endpoint: str
    auth_type: str | None = None  # "bearer" or "api_key"
    api_key_header_name: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    persist_response_headers: list[str] = Field(default_factory=list)

class RegistryLoader:
    """
    Loads and validates provider configs from registry.yaml.
    """

    def __init__(self, registry_path: str) -> None:
        self._path = Path(registry_path)
        if not self._path.exists():
            raise FileNotFoundError(f"registry.yaml not found at {registry_path}")
        self._providers: Dict[str, ProviderConfig] = {}
        self._load()

    def _load(self) -> None:
        data = yaml.safe_load(self._path.read_text())
        servers = data.get("servers") or {}
        providers: Dict[str, ProviderConfig] = {}
        for name, cfg in servers.items():
            try:
                pc = ProviderConfig(**cfg)
            except ValidationError as e:
                raise ValueError(f"Invalid provider config for {name}: {e}") from e
            providers[pc.name] = pc
        self._providers = providers

    def get_provider_config(self, name: str) -> ProviderConfig:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' not found in registry")
        return self._providers[name]

    def list_providers(self) -> Dict[str, ProviderConfig]:
        return dict(self._providers)