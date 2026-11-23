from pathlib import Path

import pytest

from ofertas.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        data={
            "user_agent": "OfertasBot-test/0.1",
            "database": ":memory:",
            "lock_file": str(tmp_path / "lock"),
            "log_file": str(tmp_path / "log"),
            "sources": {
                "promobit": {"enabled": True, "urls": ["https://www.promobit.com.br/promocoes/recentes/"]},
                "gatry": {"enabled": True, "urls": ["https://gatry.com/promocoes"]},
            },
            "stores": {"mode": "whitelist", "whitelist": ["Amazon", "Mercado Livre", "KaBuM!"], "blacklist": []},
            "criteria": {
                "resend_window_hours": 72,
                "resend_min_drop": 0.05,
                "fallback": {"min_declared_discount": 0.30, "min_votes": 15, "min_temperature": 300},
                "max_notifications_per_run": 5,
            },
            "health": {"alert_after_failures": 3},
        },
        root=tmp_path,
        telegram_token=None,
        telegram_chat_id=None,
    )
