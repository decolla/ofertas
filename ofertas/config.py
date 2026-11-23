from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass
class Config:
    data: dict[str, Any]
    root: Path
    telegram_token: str | None
    telegram_chat_id: str | None

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else self.root / p

    @property
    def user_agent(self) -> str:
        return self.data["user_agent"]


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE, # comments). Does not override real env."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(config_path: Path | None = None, root: Path | None = None) -> Config:
    root = root or ROOT
    config_path = config_path or root / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config não encontrado: {config_path}") from exc
    for required in ("user_agent", "database", "sources", "criteria", "stores"):
        if required not in data:
            raise ConfigError(f"config.yaml sem a chave obrigatória '{required}'")
    if data["stores"].get("mode") not in ("whitelist", "blacklist"):
        raise ConfigError("stores.mode deve ser 'whitelist' ou 'blacklist'")

    env = load_dotenv(root / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")})
    return Config(
        data=data,
        root=root,
        telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID") or None,
    )
