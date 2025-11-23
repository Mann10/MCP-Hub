from typing import Dict, Any

from .registry_loader import ProviderConfig


class AuthManager:
    """
    Builds auth headers for backend MCP servers.
    """

    def build_headers(self, provider: ProviderConfig, credentials: Dict[str, Any]) -> Dict[str, str]:
        """
        Given provider config and user credentials,
        return a dict of headers for httpx.AsyncClient.

        Supported:
        - bearer: expects credentials["token"]
        - api_key: expects credentials["api_key"] or ["key"] or ["token"]
        - none / missing: no auth headers
        """
        headers: Dict[str, str] = dict(provider.extra_headers or {})
        auth_type = (provider.auth_type or "none").lower()
        # No-auth providers
        if auth_type in ("none", ""):
            return headers

        if auth_type == "bearer":
            token = credentials.get("token")
            if not token:
                raise ValueError(f"Missing 'token' for bearer auth (provider={provider.name})")
            print(f'token is {token}')
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key":
            key = (
                credentials.get("api_key")
                or credentials.get("key")
                or credentials.get("token")
            )
            if not key:
                raise ValueError(
                    f"Missing API key credential for provider={provider.name} "
                    "(expected 'api_key' or 'key' or 'token')"
                )
            header_name = provider.api_key_header_name or "x-api-key"
            headers[header_name] = key

        else:
            raise ValueError(f"Unsupported auth_type '{provider.auth_type}' for provider={provider.name}")

        return headers