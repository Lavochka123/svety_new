# svety/core/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    SECRET_KEY: str
    DOMAIN: str
    DATA_DIR: Path
    MAX_UPLOAD_MB: int
    PORT: int

    @staticmethod
    def _int(value: str | None, default: int) -> int:
        try:
            return int(str(value).strip()) if value is not None else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        secret = os.getenv("SECRET_KEY", "change_me")
        domain = os.getenv("DOMAIN", "")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        max_upload = cls._int(os.getenv("MAX_UPLOAD_MB"), 10)
        port = cls._int(os.getenv("PORT"), 5000)

        # создаём директорию хранения, если нет
        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            SECRET_KEY=secret,
            DOMAIN=domain,
            DATA_DIR=data_dir,
            MAX_UPLOAD_MB=max_upload,
            PORT=port,
        )


# Единый объект конфигурации для импорта
cfg: Config = Config.from_env()
