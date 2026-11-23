import pytest

from ofertas.bot import enabled_categories, handle_update, min_score, run_bot
from ofertas.storage import Storage

CHAT = "5752633371"


class FakeTG:
    def __init__(self, updates=None):
        self.sent, self.calls = [], []
        self.updates = updates or []

    def send_text(self, text, button=None):
        self.sent.append(text)
        return 1

    def call(self, method, payload, timeout=None):
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return True


@pytest.fixture
def env(cfg):
    cfg.telegram_chat_id = CHAT
    cfg.data["categories"] = {"enabled": ["hardware", "cozinha"]}
    cfg.data["criteria"]["min_score"] = 80
    cfg.data["claude"] = {"daily_call_limit": 100}
    return cfg, Storage(":memory:"), FakeTG()


def msg(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def test_pause_and_resume(env):
    cfg, db, tg = env
    handle_update(msg("/pausar"), cfg, db, tg)
    assert db.get_setting("paused") == "1" and "pausados" in tg.sent[-1]
    handle_update(msg("/retomar"), cfg, db, tg)
    assert db.get_setting("paused") == "0"


def test_ignores_other_chats(env):
    cfg, db, tg = env
    handle_update(msg("/pausar", chat="999"), cfg, db, tg)
    assert db.get_setting("paused") is None and tg.sent == []


@pytest.mark.parametrize("text,expected,reply", [
    ("/minimo", 80, "Nota mínima atual: <b>80</b>"),
    ("/minimo 75", 75, "agora é <b>75</b>"),
    ("/minimo 150", 80, "de 0 a 100"),
    ("/minimo abc", 80, "de 0 a 100"),
])
def test_minimo(env, text, expected, reply):
    cfg, db, tg = env
    handle_update(msg(text), cfg, db, tg)
    assert min_score(cfg, db) == expected and reply in tg.sent[-1]


def test_categories_toggle_via_buttons(env):
    cfg, db, tg = env
    handle_update(msg("/categorias"), cfg, db, tg)
    method, payload = tg.calls[-1]
    buttons = [b[0]["text"] for b in payload["reply_markup"]["inline_keyboard"]]
    assert "✅ 🖥️ Peças de PC" in buttons and "⬜ 🏠 Casa" in buttons

    cq = {"update_id": 2, "callback_query": {"id": "q", "data": "cat:casa",
          "message": {"message_id": 7, "chat": {"id": int(CHAT)}}}}
    handle_update(cq, cfg, db, tg)
    assert enabled_categories(cfg, db) == ["hardware", "casa", "cozinha"]
    handle_update({**cq, "callback_query": {**cq["callback_query"], "data": "cat:hardware"}}, cfg, db, tg)
    assert enabled_categories(cfg, db) == ["casa", "cozinha"]
    assert [m for m, _ in tg.calls].count("answerCallbackQuery") == 2


def test_bogus_callback_is_ignored(env):
    cfg, db, tg = env
    cq = {"update_id": 2, "callback_query": {"id": "q", "data": "cat:'; DROP TABLE",
          "message": {"message_id": 7, "chat": {"id": int(CHAT)}}}}
    handle_update(cq, cfg, db, tg)
    assert db.get_setting("categories") is None


def test_status_mentions_state(env):
    cfg, db, tg = env
    db.source_failed("kabum", "blocked")
    handle_update(msg("/status"), cfg, db, tg)
    assert "Nota mínima: 80" in tg.sent[-1] and "kabum: ⚠️ 1 falha(s)" in tg.sent[-1]


def test_run_bot_persists_offset(env):
    cfg, db, tg = env
    tg.updates = [{**msg("/pausar"), "update_id": 41}]
    run_bot(cfg, db, tg, once=True)
    assert db.get_setting("tg_offset") == "42" and db.get_setting("paused") == "1"
    assert tg.calls[0][0] == "setMyCommands"


def test_runner_uses_bot_overrides(env):
    """The /minimo and /categorias settings change what the runner sends."""
    from ofertas.runner import judge
    from ofertas.models import Decision, RawOffer, Verdict

    cfg, db, tg = env
    cfg.data["claude"]["enabled"] = True
    off = db.upsert_offer(RawOffer(source="promobit", source_id="1", title="X", price=10, url="u"))
    db.save_verdict(off.id, 10, Verdict(score=76, fake=False, flags=[], category="casa",
                                        name_norm="X", condition="novo", reason="r"))
    pre = [(off, Decision(ok=True))]
    assert not judge(cfg, db, None, pre)[0][1].ok          # 76 < 80, casa disabled
    db.set_setting("min_score", "75")
    db.set_setting("categories", "casa")
    assert judge(cfg, db, None, pre)[0][1].ok
