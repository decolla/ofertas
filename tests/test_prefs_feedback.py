"""Wishlist (/quero), mute (/silenciar), 👍/👎 feedback, ended deals and the
daily digest."""

import json
from datetime import date, datetime, timezone

import httpx
import pytest

from ofertas import criteria, prefs
from ofertas.bot import handle_update
from ofertas.models import Decision, RawOffer, StoredOffer
from ofertas.notify import ENDED_LABEL, Telegram
from ofertas.runner import maybe_digest, run
from ofertas.sources.ofertaesperta import _to_offer as oe_offer
from ofertas.sources.pelando import PelandoSource
from ofertas.sources.promobit import PromobitSource
from ofertas.storage import Storage
from ofertas.triage import Triage
from tests.conftest import fixture_text
from tests.test_bot import CHAT, FakeTG
from tests.test_http_and_runner import ROUTES_OK, FakeTelegram, client_for, html, promobit_with

FAN, WELLA = "3242903", "3242887"  # 74% off Amazon fan; 34% off, 1 like, Mercado Livre


# -- term matching / storage ---------------------------------------------------
@pytest.mark.parametrize("term,title,ok", [
    ("air fryer", "Fritadeira Air Fryer Mondial 4L", True),
    ("Fryer AIR", "fritadeira air-fryer", True),
    ("catan", "Jogo de Tabuleiro Catan Base", True),
    ("pão", "Pao de queijo congelado", True),
    ("air fryer", "Fritadeira elétrica Mondial", False),
    ("de", "Pão de queijo", False),  # stopword only: matches nothing
])
def test_matches(term, title, ok):
    assert prefs.matches(term, title) is ok


def test_add_list_remove_terms():
    db = Storage(":memory:")
    assert prefs.add_term(db, prefs.WISHLIST, "  air   fryer ") == (True, "air fryer")
    assert prefs.add_term(db, prefs.WISHLIST, "AIR FRYER")[0] is False  # duplicate
    prefs.add_term(db, prefs.WISHLIST, "catan")
    assert prefs.get_terms(db, prefs.WISHLIST) == ["air fryer", "catan"]
    assert prefs.remove_term(db, prefs.WISHLIST, "2") == "catan"
    assert prefs.remove_term(db, prefs.WISHLIST, "Air Fryer") == "air fryer"
    assert prefs.remove_term(db, prefs.WISHLIST, "nada") is None
    assert prefs.get_terms(db, prefs.MUTED) == []


def test_wish_cfg_relaxes_but_keeps_community_scales():
    base = {"min_discount_vs_median": 0.25, "min_history_points": 5, "fallback": {
        "min_declared_discount_alone": 0.4, "min_declared_discount": 0.3,
        "per_source": {"kabum": {"min_declared_discount_alone": 0.6},
                       "pelando": {"min_temperature": 150}}}}
    w = criteria.wish_cfg(base, {"min_discount_vs_median": 0.1, "min_declared_discount": 0.2})
    assert w["min_discount_vs_median"] == 0.1 and w["min_history_points"] == 3
    assert w["fallback"]["min_declared_discount_alone"] == 0.2
    assert w["fallback"]["per_source"] == {"kabum": {}, "pelando": {"min_temperature": 150}}
    assert base["fallback"]["min_declared_discount_alone"] == 0.4  # not mutated


def test_kabum_needs_60_percent_without_history():
    fb = {"min_declared_discount_alone": 0.4, "min_declared_discount": 0.3,
          "per_source": {"kabum": {"min_declared_discount_alone": 0.6}}}
    ccfg = {"fallback": fb}
    stores = {"mode": "blacklist", "blacklist": []}
    off = lambda lp: RawOffer(source="kabum", source_id="1", title="SSD", price=100,  # noqa: E731
                              url="u", store="KaBuM!", list_price=lp)
    assert not criteria.objective(off(190), ccfg, stores).ok   # 47%
    assert criteria.objective(off(260), ccfg, stores).ok       # 62%


# -- runner: wish, mute, ended -------------------------------------------------
def routes(**fan):
    r = dict(ROUTES_OK)
    r["https://www.promobit.com.br/promocoes/recentes/"] = html(promobit_with(offerLikes=40, **fan))
    return r


def test_wish_passes_with_lighter_criterion_and_is_labelled(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    prefs.add_term(db, prefs.WISHLIST, "condicionador wella")
    run(cfg, client_for(routes()), db, tg)
    assert [o.raw.source_id for o in tg.offers] == [FAN, WELLA]
    assert tg.decisions[1].wish == "condicionador wella"


def test_muted_term_is_blocked_before_claude(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    prefs.add_term(db, prefs.MUTED, "ventilador")
    rep = run(cfg, client_for(routes()), db, tg)
    assert tg.offers == []
    fan = next(d for o, d in rep.evaluated if o.raw.source_id == FAN)
    assert "silenciado: ventilador" in fan.reasons and not fan.pending


def test_ended_deal_edits_the_sent_message_and_is_not_judged(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    run(cfg, client_for(routes()), db, tg)
    assert [o.raw.source_id for o in tg.offers] == [FAN]

    rep = run(cfg, client_for(routes(offerStatusName="FINISHED")), db, tg)
    assert tg.closed == [101] and rep.closed == 1
    assert all(o.raw.source_id != FAN for o, _ in rep.evaluated)
    assert db.conn.execute("SELECT ended_at FROM sent").fetchone()[0] is not None

    run(cfg, client_for(routes(offerStatusName="FINISHED")), db, tg)
    assert tg.closed == [101]  # only once


class RecordingCLI:
    """Scores the wish 70 and anything else 70; records prompts."""

    def __init__(self, score=70):
        self.prompts, self.score = [], score

    def __call__(self, prompt):
        self.prompts.append(prompt)
        offers = json.loads(prompt.split("Avalie estas ofertas:\n", 1)[1])
        return {"subtype": "success", "is_error": False, "total_cost_usd": 0.001,
                "structured_output": {"items": [
                    {"id": o["id"], "category": "casa", "name_norm": o["title"][:40],
                     "condition": "novo", "fake": False, "flags": [], "score": self.score,
                     "reason": "ok"} for o in offers]}}


def test_wish_goes_first_to_claude_with_lower_min_score_and_feedback(cfg):
    cfg.data["claude"] = {"enabled": True}
    cfg.data["criteria"]["min_score"] = 80
    cfg.data["categories"] = {"enabled": ["casa"]}
    db, tg, cli = Storage(":memory:"), FakeTelegram(), RecordingCLI(score=70)
    prefs.add_term(db, prefs.WISHLIST, "condicionador wella")
    # an old 👎 on some offer, so the prompt carries the user's taste
    old = db.upsert_offer(RawOffer(source="gatry", source_id="x", title="Cafeteira Velha",
                                   price=99, url="https://g/x", store="Amazon"))
    db.set_feedback(old.id, -1)
    tri = Triage(cli, db, daily_limit=10, today=lambda: date(2026, 11, 24))
    run(cfg, client_for(routes()), db, tg, triage=tri)

    prompt = cli.prompts[0]
    assert "gosto_do_usuario" in prompt and "Cafeteira Velha" in prompt and "👎" in prompt
    ids = [o["id"] for o in json.loads(prompt.split("Avalie estas ofertas:\n", 1)[1])]
    wella_id = db.conn.execute("SELECT id FROM offers WHERE source_id = ?", (WELLA,)).fetchone()[0]
    assert ids[0] == str(wella_id)
    assert [o.raw.source_id for o in tg.offers] == [WELLA]  # 70 ≥ 65 (wish); fan 70 < 80


# -- notify: ended edit --------------------------------------------------------
def tg_calls(ok=True):
    calls = []

    def handler(req):
        calls.append((req.url.path.rsplit("/", 1)[1], json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    return Telegram("T", "42", transport=httpx.MockTransport(handler)), calls


@pytest.mark.parametrize("photo,text,method", [
    (True, "<b>Mouse</b> curto", "editMessageCaption"),
    (True, "x" * 1020, "editMessageReplyMarkup"),  # prefix would overflow the caption
    (False, "<b>Mouse</b>", "editMessageText"),
])
def test_close_offer(photo, text, method):
    tg, calls = tg_calls()
    tg.close_offer(7, text, photo, "https://loja/x", 3)
    (m, body), = calls
    assert m == method and body["message_id"] == 7
    assert body["reply_markup"]["inline_keyboard"][0][0]["text"] == ENDED_LABEL
    if method != "editMessageReplyMarkup":
        assert "Oferta encerrada" in (body.get("caption") or body.get("text"))


# -- parsers: ended flags --------------------------------------------------------
def test_promobit_finished_is_ended():
    offers = PromobitSource.parse(promobit_with(offerStatusName="FINISHED"))
    assert {o.source_id for o in offers if o.ended} == {FAN}


def test_pelando_expired_is_ended():
    page = fixture_text("pelando_recentes.html")
    at = page.index("7ad19ed5-7663-41ba-9135-fdb8e561ed1c")  # first promotion
    head, tail = page[:at], page[at:]
    tail = tail.replace("&quot;status&quot;: [0, &quot;active&quot;]",
                        "&quot;status&quot;: [0, &quot;expired&quot;]", 1)
    offers = PelandoSource.parse(head + tail)
    assert len(offers) == 10 and [o.ended for o in offers][0] is True
    assert sum(o.ended for o in offers) == 1


def test_ofertaesperta_encerrada_is_ended():
    o = oe_offer({"id": "a", "title": "X", "priceNow": "R$ 10", "encerrada": True}, {})
    assert o.ended and o.price == 10


# -- bot ---------------------------------------------------------------------------
@pytest.fixture
def bot_env(cfg):
    cfg.telegram_chat_id = CHAT
    return cfg, Storage(":memory:"), FakeTG()


def msg(text):
    return {"message": {"chat": {"id": int(CHAT)}, "text": text}}


def test_feedback_button_stores_vote_and_marks_choice(bot_env):
    cfg, db, tg = bot_env
    off = db.upsert_offer(RawOffer(source="gatry", source_id="1", title="Mouse", price=50,
                                   url="https://g/1", store="Amazon"))
    markup = {"inline_keyboard": [[{"text": "🛒 Abrir oferta", "url": "https://g/1"}],
                                  [{"text": "👍 Boa", "callback_data": f"fb:{off.id}:1"},
                                   {"text": "👎 Não curti", "callback_data": f"fb:{off.id}:-1"}]]}
    handle_update({"callback_query": {"id": "q", "data": f"fb:{off.id}:-1", "message": {
        "chat": {"id": int(CHAT)}, "message_id": 9, "reply_markup": markup}}}, cfg, db, tg)
    assert db.conn.execute("SELECT vote FROM feedback").fetchone()[0] == -1
    (m1, edit), (m2, _) = tg.calls
    assert m1 == "editMessageReplyMarkup" and m2 == "answerCallbackQuery"
    rows = edit["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["url"] == "https://g/1" and rows[1][1]["text"].startswith("✅")


def test_feedback_from_other_chat_is_ignored(bot_env):
    cfg, db, tg = bot_env
    handle_update({"callback_query": {"id": "q", "data": "fb:1:1", "message": {
        "chat": {"id": 1}, "message_id": 9}}}, cfg, db, tg)
    assert tg.calls == [] and db.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_wishlist_commands(bot_env):
    cfg, db, tg = bot_env
    handle_update(msg("/quero air fryer"), cfg, db, tg)
    handle_update(msg("/quero"), cfg, db, tg)
    handle_update(msg("/naoquero 1"), cfg, db, tg)
    handle_update(msg("/silenciar <b>iphone</b>"), cfg, db, tg)
    assert "lista de desejos" in tg.sent[0] and "1. air fryer" in tg.sent[1]
    assert "Removido" in tg.sent[2] and prefs.get_terms(db, prefs.WISHLIST) == []
    assert "&lt;b&gt;" in tg.sent[3]  # user text is escaped in the HTML reply


def test_resumo_lists_near_misses(bot_env):
    cfg, db, tg = bot_env
    cfg.data["criteria"]["min_score"] = 80
    from ofertas.models import Verdict

    off = db.upsert_offer(RawOffer(source="gatry", source_id="1", title="Air Fryer Mondial",
                                   price=199, url="https://g/1", store="Amazon"))
    db.save_verdict(off.id, 199, Verdict(score=74, fake=False, flags=[], category="cozinha",
                                         name_norm="Air Fryer Mondial 4L", condition="novo",
                                         reason="bom"))
    handle_update(msg("/resumo"), cfg, db, tg)
    assert "Air Fryer Mondial 4L" in tg.sent[0] and "nota 74" in tg.sent[0]
    assert "/minimo 74" in tg.sent[0]


# -- digest timing -----------------------------------------------------------------
def at_local(hour):
    return datetime.now().astimezone().replace(hour=hour, minute=30).astimezone(timezone.utc)


def test_digest_once_a_day_after_the_hour(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    cfg.data["digest"] = {"hour": 21}
    assert not maybe_digest(cfg, db, tg, at_local(20))
    assert maybe_digest(cfg, db, tg, at_local(21))
    assert not maybe_digest(cfg, db, tg, at_local(22))
    assert len(tg.texts) == 1 and "Resumo do dia" in tg.texts[0]


def test_no_digest_when_paused(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    db.set_setting("paused", "1")
    assert not maybe_digest(cfg, db, tg, at_local(22)) and tg.texts == []


# -- per-category and favourite-brand minimum score ------------------------------
OFFSETS = {"min_score_offsets": {"calcados": -8, "jogos_tabuleiro": -10}, "min_score_floor": 65}


@pytest.mark.parametrize("base,cat,brand,wish,need", [
    (80, "hardware", False, None, 80),
    (80, "calcados", False, None, 72),
    (80, "calcados", True, None, 69),        # -8 category, -3 brand
    (70, "jogos_tabuleiro", True, None, 65),  # floor
    (60, "calcados", False, None, 60),        # /minimo below the floor is respected
    (80, "hardware", False, 65, 65),          # /quero
    (80, "calcados", True, 70, 69),           # the lower of the two
])
def test_required_score(base, cat, brand, wish, need):
    assert criteria.required_score(base, cat, brand, OFFSETS, brand_offset=-3,
                                   wish_score=wish) == need


def test_favourite_brand_lowers_min_score_and_shows_in_message(cfg):
    cfg.data["claude"] = {"enabled": True}
    cfg.data["criteria"]["min_score"] = 80
    cfg.data["categories"] = {"enabled": ["casa"]}
    tri = lambda db: Triage(RecordingCLI(score=75), db, daily_limit=10,  # noqa: E731
                            today=lambda: date(2026, 11, 25))
    db, tg = Storage(":memory:"), FakeTelegram()
    run(cfg, client_for(routes()), db, tg, triage=tri(db))
    assert tg.offers == []  # 75 < 80

    cfg.data["brands"] = {"favorites": ["Mondial"], "score_offset": -6}
    db, tg = Storage(":memory:"), FakeTelegram()
    run(cfg, client_for(routes()), db, tg, triage=tri(db))
    assert [o.raw.source_id for o in tg.offers] == [FAN] and tg.decisions[0].brand == "Mondial"

    from ofertas.notify import format_offer
    assert "🏷️ Marca favorita: Mondial" in format_offer(tg.offers[0], tg.decisions[0])


# -- regressions seen live on 2026-11-25 -----------------------------------------
@pytest.mark.parametrize("store,ok", [
    ("EA Games", False), ("EA", False), ("ea app", False), ("Steam", False),
    ("Eataly", True), ("Amazon", True), (None, True),
])
def test_blacklist_also_blocks_longer_names(store, ok):
    stores = {"mode": "blacklist", "blacklist": ["EA", "Steam"]}
    assert criteria.store_allowed(store, stores) is ok


def test_repost_with_other_title_is_not_sent_again_in_the_window(cfg):
    """Baby Alive went out twice: same product, new offer id and title."""
    from tests.test_http_and_runner import CountingCLI, claude_cfg

    cfg, db, tg = claude_cfg(cfg), Storage(":memory:"), FakeTelegram()
    tri = Triage(CountingCLI(score=90), db, daily_limit=10, today=lambda: date(2026, 11, 25))
    run(cfg, client_for(routes()), db, tg, triage=tri)
    assert [o.raw.source_id for o in tg.offers] == [FAN]

    repost = routes(offerId=999, offerTitle="Ventilador de Mesa Mondial 30 cm 110V")
    run(cfg, client_for(repost), db, tg, triage=tri)
    assert len(tg.offers) == 1  # CountingCLI names both "Ventilador Mondial 30cm"

    cheaper = routes(offerId=998, offerTitle="Ventilador Mondial 30cm Turbo", offerPrice=60.0)
    run(cfg, client_for(cheaper), db, tg, triage=tri)
    assert [o.raw.source_id for o in tg.offers] == [FAN, "998"]  # 17% cheaper: allowed


# -- Claude subscription limit ("You've hit your 5-hour limit") ------------------
from ofertas.triage import COOLDOWN_KEY, parse_reset  # noqa: E402

LIMIT_MSG = "You've hit your 5-hour limit · resets 2pm (America/Sao_Paulo)"


def limited_cli(prompt):
    return {"subtype": "success", "is_error": True, "result": LIMIT_MSG}


@pytest.mark.parametrize("text,now,expected", [
    (LIMIT_MSG, datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc),   # 12:00 in SP
     datetime(2026, 11, 26, 17, 0, tzinfo=timezone.utc)),             # 14:00 in SP
    (LIMIT_MSG, datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc),   # 15:00: tomorrow
     datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)),
    ("resets 9:30am (America/Sao_Paulo)", datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc),
     datetime(2026, 11, 26, 12, 30, tzinfo=timezone.utc)),
    ("limit reached", datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc), None),
])
def test_parse_reset(text, now, expected):
    assert parse_reset(text, now) == expected


def test_rate_limit_pauses_refunds_and_alerts_once():
    db, alerts = Storage(":memory:"), []
    now = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
    calls = []

    def cli(prompt):
        calls.append(prompt)
        return limited_cli(prompt)

    tri = Triage(cli, db, daily_limit=10, batch_size=1, on_limit=alerts.append,
                 today=lambda: date(2026, 11, 26), now=lambda: now)
    offers = [db.upsert_offer(RawOffer(source="gatry", source_id=str(i), title=f"X {i}",
                                       price=10, url=f"https://g/{i}")) for i in range(3)]
    assert tri.judge(offers) == {} and tri.rate_limited
    assert len(calls) == 1                      # stopped at the first refusal
    assert db.claude_calls(date(2026, 11, 26)) == 0  # refunded
    assert db.get_setting(COOLDOWN_KEY).startswith("2026-11-26T17:00")
    assert len(alerts) == 1 and "limite de uso" in alerts[0]

    tri2 = Triage(cli, db, daily_limit=10, today=lambda: date(2026, 11, 26), now=lambda: now,
                  on_limit=alerts.append)
    assert tri2.judge(offers) == {} and len(calls) == 1 and len(alerts) == 1  # cooling down
    later = Triage(lambda p: {"subtype": "success", "is_error": False, "structured_output": {"items": []}},
                   db, daily_limit=10, today=lambda: date(2026, 11, 26),
                   now=lambda: datetime(2026, 11, 26, 17, 1, tzinfo=timezone.utc))
    assert later.available()


def test_rate_limited_sends_only_history_backed_offers_flagged(cfg):
    from datetime import timedelta

    from ofertas.notify import format_offer

    cfg.data["claude"] = {"enabled": True}
    cfg.data["criteria"].update({"min_history_points": 3, "history_days": 90,
                                 "min_discount_vs_median": 0.25, "min_score": 80})
    page = fixture_text("gatry_promocoes.html")
    db, tg = Storage(":memory:"), FakeTelegram()
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    r = routes()
    r["https://gatry.com/promocoes"] = html(page)
    for day in range(4):  # history at R$ 899, no Claude yet
        run(cfg, client_for(r), db, tg, now=t0 + timedelta(days=day))
    tg.offers.clear()
    r["https://gatry.com/promocoes"] = html(page.replace("R$ 899,00", "R$ 599,00"))
    tri = Triage(limited_cli, db, daily_limit=10, today=lambda: date(2026, 11, 6),
                 now=lambda: t0 + timedelta(days=5))
    rep = run(cfg, client_for(r), db, tg, triage=tri, now=t0 + timedelta(days=5))
    sent = [o.raw.title[:15] for o in tg.offers]
    assert sent == ["Monitor Samsung"]  # the fan (declared discount only) waits
    d = tg.decisions[-1]
    assert d.unjudged and "Sem avaliação do Claude" in format_offer(tg.offers[0], d)


def test_rate_limited_offer_without_history_waits(cfg):
    cfg.data["claude"] = {"enabled": True}
    db, tg = Storage(":memory:"), FakeTelegram()
    tri = Triage(limited_cli, db, daily_limit=10, today=lambda: date(2026, 11, 26))
    rep = run(cfg, client_for(routes()), db, tg, triage=tri)
    assert tg.offers == []  # the fan passes on declared discount only
    fan = next(d for o, d in rep.evaluated if o.raw.source_id == FAN)
    assert fan.pending and not fan.ok and not fan.unjudged


# -- per-source time budget ------------------------------------------------------
def test_source_time_budget_stops_rotation_without_failing():
    from types import SimpleNamespace

    from ofertas.models import RawOffer
    from ofertas.sources.base import Source

    clock = {"t": 0.0}
    got = []

    class SlowClient:
        def get(self, url):
            got.append(url)
            clock["t"] += 100  # every page takes 100 s
            return SimpleNamespace(text=url, raise_for_status=lambda: None)

    class S(Source):
        name = "slow"

        @staticmethod
        def parse(html):
            return [RawOffer(source="slow", source_id=html, title=html, price=10.0, url=html)]

    src = S(SlowClient(), ["a", "b", "c", "d"], max_seconds=250, clock=lambda: clock["t"])
    offers = src.fetch()
    assert got == ["a", "b", "c"] and len(offers) == 3  # 4th page skipped, no error
