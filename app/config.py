import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel

# Load .env if present
load_dotenv()


class Settings(BaseModel):
    database_url: str
    registry_path: str
    backend_timeout: float = 10.0
    retry_attempts: int = 2
    retry_backoff_base: float = 0.5

    class Config:
        arbitrary_types_allowed = True


def _default_registry_path() -> str:
    here = Path(__file__).resolve().parent
    return str(here / "registry.yaml")


settings = Settings(
    database_url=os.getenv("DATABASE_URL", "sqlite:///./gateway.db"),
    registry_path=os.getenv("REGISTRY_PATH", _default_registry_path()),
    backend_timeout=float(os.getenv("BACKEND_TIMEOUT", "10")),
    retry_attempts=int(os.getenv("RETRY_ATTEMPTS", "2")),
    retry_backoff_base=float(os.getenv("RETRY_BACKOFF_BASE", "0.5")),
)