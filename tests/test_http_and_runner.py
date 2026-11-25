import json

import httpx
import pytest

from ofertas.http import BlockedError, FetchError, PoliteClient
from ofertas.runner import ConsoleNotifier, run
from ofertas.storage import Storage
from tests.conftest import fixture_text

ROBOTS_OK = "User-agent: *\nAllow: /\n"


def client_for(routes: dict, calls: list | None = None) -> PoliteClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        key = str(request.url)
        if key in routes:
            r = routes[key]
            return r(request) if callable(r) else r
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        return httpx.Response(404, text="not found")

    return PoliteClient("test-ua", retries=2, backoff_base=0, min_interval=0,
                        transport=httpx.MockTransport(handler), sleep=lambda s: None)


def html(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/html"})


# -- http ------------------------------------------------------------------
def test_robots_disallow_blocks():
    c = client_for({"https://a.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /p\n")})
    with pytest.raises(BlockedError, match="robots"):
        c.get("https://a.com/p/1")


def test_cloudflare_challenge_is_blocked_not_retried():
    calls = []
    c = client_for({"https://a.com/x": html("<html><title>Just a moment...</title></html>")}, calls)
    with pytest.raises(BlockedError):
        c.get("https://a.com/x")
    assert calls.count("https://a.com/x") == 1


def test_retries_5xx_then_succeeds():
    seq = iter([httpx.Response(503), httpx.Response(502), html("ok")])
    c = client_for({"https://a.com/x": lambda r: next(seq)})
    assert c.get("https://a.com/x").text == "ok"


def test_gives_up_after_retries():
    c = client_for({"https://a.com/x": httpx.Response(500)})
    with pytest.raises(FetchError):
        c.get("https://a.com/x")


def test_sends_honest_user_agent():
    seen = {}

    def capture(req):
        seen["ua"] = req.headers["user-agent"]
        return html("ok")

    client_for({"https://a.com/x": capture}).get("https://a.com/x")
    assert seen["ua"] == "test-ua"


# -- runner ----------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.texts, self.offers = [], []

    def send_text(self, text, button=None):
        self.texts.append(text)
        return len(self.texts)

    def send_offer(self, offer, decision):
        from ofertas.notify import SentMessage

        self.offers.append(offer)
        self.decisions = [*getattr(self, "decisions", []), decision]
        return SentMessage(100 + len(self.offers), f"msg {offer.raw.title}", photo=False)

    def close_offer(self, message_id, text, photo, url, offer_id):
        self.closed = [*getattr(self, "closed", []), message_id]


def promobit_with(**overrides) -> str:
    """Promobit fixture with the fan offer tweaked to (not) pass the criterion."""
    page = fixture_text("promobit_recentes.html")
    start = page.index('id="__NEXT_DATA__" type="application/json">') + len('id="__NEXT_DATA__" type="application/json">')
    end = page.index("</script>", start)
    data = json.loads(page[start:end])
    for o in data["props"]["pageProps"]["serverOffers"]["offers"]:
        if o["offerId"] == 3242903:
            o.update(overrides)
    return page[:start] + json.dumps(data) + page[end:]


ROUTES_OK = {
    "https://www.promobit.com.br/promocoes/recentes/": html(promobit_with(offerLikes=40)),
    "https://gatry.com/promocoes": html(fixture_text("gatry_promocoes.html")),
}


def test_run_sends_only_passing_offer_and_dedupes(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    rep = run(cfg, client_for(ROUTES_OK), db, tg)
    assert rep.collected == {"promobit": 12, "gatry": 40}
    assert [o.raw.source_id for o in tg.offers] == ["3242903"]  # 74% off, 40 likes, Amazon

    rep2 = run(cfg, client_for(ROUTES_OK), db, tg)  # nothing new or cheaper
    assert rep2.sent == [] and len(tg.offers) == 1


def test_broken_source_does_not_stop_others_and_alerts_after_threshold(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    routes = dict(ROUTES_OK)
    routes["https://gatry.com/promocoes"] = httpx.Response(403)
    for _ in range(3):
        rep = run(cfg, client_for(routes), db, tg)
        assert "gatry" in rep.failed and rep.collected["promobit"] == 12
    assert len([t for t in tg.texts if "gatry" in t and "falhou" in t]) == 1

    run(cfg, client_for(ROUTES_OK), db, tg)
    assert any("voltou a funcionar" in t for t in tg.texts)


def test_dry_run_prints_and_writes_nothing(cfg):
    real, out = Storage(":memory:"), []
    rep = run(cfg, client_for(ROUTES_OK), real.snapshot(), ConsoleNotifier(out.append), dry_run=True)
    db = real
    assert len(rep.sent) == 1 and "Mondial Ventilador" in out[0] and "<b>" not in out[0]
    assert db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0


def test_paused_collects_but_does_not_send(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    db.set_setting("paused", "1")
    run(cfg, client_for(ROUTES_OK), db, tg)
    assert tg.offers == []
    assert db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 52


# -- runner + Claude ---------------------------------------------------------
from datetime import date  # noqa: E402

from ofertas.triage import Triage  # noqa: E402


class CountingCLI:
    def __init__(self, score):
        self.score, self.calls = score, 0

    def __call__(self, prompt):
        self.calls += 1
        ids = [o["id"] for o in json.loads(prompt.split("\n", 1)[1])]
        return {"subtype": "success", "is_error": False, "total_cost_usd": 0.001,
                "structured_output": {"items": [
                    {"id": i, "category": "casa", "name_norm": "Ventilador Mondial 30cm",
                     "condition": "novo", "fake": False, "flags": [], "score": self.score,
                     "reason": "Excelente preço"} for i in ids]}}


def claude_cfg(cfg):
    cfg.data["claude"] = {"enabled": True}
    cfg.data["criteria"]["min_score"] = 80
    cfg.data["categories"] = {"enabled": ["casa", "cozinha"]}
    return cfg


def test_claude_only_sees_objective_passes_and_is_cached(cfg):
    cfg, db, tg = claude_cfg(cfg), Storage(":memory:"), FakeTelegram()
    cli = CountingCLI(score=90)
    tri = Triage(cli, db, daily_limit=10, today=lambda: date(2026, 11, 23))
    run(cfg, client_for(ROUTES_OK), db, tg, triage=tri)
    assert cli.calls == 1 and [o.raw.source_id for o in tg.offers] == ["3242903"]
    run(cfg, client_for(ROUTES_OK), db, tg, triage=tri)
    assert cli.calls == 1  # cached verdict, deduped send


def test_low_claude_score_blocks_send(cfg):
    cfg, db, tg = claude_cfg(cfg), Storage(":memory:"), FakeTelegram()
    tri = Triage(CountingCLI(score=60), db, daily_limit=10, today=lambda: date(2026, 11, 23))
    rep = run(cfg, client_for(ROUTES_OK), db, tg, triage=tri)
    assert tg.offers == []
    assert any("nota 60 < 80" in d.reasons for _, d in rep.evaluated)


def test_no_triage_leaves_offer_pending(cfg):
    cfg, db, tg = claude_cfg(cfg), Storage(":memory:"), FakeTelegram()
    rep = run(cfg, client_for(ROUTES_OK), db, tg, triage=None)
    assert tg.offers == [] and sum(d.pending for _, d in rep.evaluated) == 1


def test_history_built_across_runs_triggers_on_price_drop(cfg):
    """Gatry has no 'de' price: only our own history can make it pass."""
    from datetime import datetime, timedelta, timezone

    cfg.data["criteria"].update({"min_history_points": 3, "history_days": 90,
                                 "min_discount_vs_median": 0.25})
    page = fixture_text("gatry_promocoes.html")
    db, tg = Storage(":memory:"), FakeTelegram()
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    routes = dict(ROUTES_OK)
    routes["https://gatry.com/promocoes"] = html(page)
    for day in range(4):  # 4 daily observations at R$ 899
        run(cfg, client_for(routes), db, tg, now=t0 + timedelta(days=day))
    assert not any(o.raw.source == "gatry" for o in tg.offers)

    routes["https://gatry.com/promocoes"] = html(page.replace("R$ 899,00", "R$ 599,00"))
    rep = run(cfg, client_for(routes), db, tg, now=t0 + timedelta(days=5))
    sent = [o for o in rep.sent if o.raw.source == "gatry"]
    assert [o.raw.title[:15] for o in sent] == ["Monitor Samsung"]
    (_, d), = [(o, d) for o, d in rep.evaluated if o.id == sent[0].id]
    assert d.stats.median == 899.0 and not d.insufficient_history


def test_same_product_different_titles_sent_once(cfg):
    """Three posts of one TV, different titles: Claude's name_norm merges them."""
    cfg = claude_cfg(cfg)
    page = promobit_with(offerLikes=40)
    # Make a second offer identical in price/discount but with another title.
    data_start = page.index('application/json">') + len('application/json">')
    data = json.loads(page[data_start:page.index("</script>", data_start)])
    offers = data["props"]["pageProps"]["serverOffers"]["offers"]
    fan = next(o for o in offers if o["offerId"] == 3242903)
    twin = dict(fan, offerId=999, offerTitle="Ventilador Mesa Mondial 30 cm 110 V", offerSlug="x-999")
    offers.append(twin)
    page = page[:data_start] + json.dumps(data) + page[page.index("</script>", data_start):]
    routes = dict(ROUTES_OK)
    routes["https://www.promobit.com.br/promocoes/recentes/"] = html(page)
    db, tg = Storage(":memory:"), FakeTelegram()
    tri = Triage(CountingCLI(score=90), db, daily_limit=10, today=lambda: date(2026, 11, 23))
    run(cfg, client_for(routes), db, tg, triage=tri)
    assert len(tg.offers) == 1


def promobit_variants(*variants: tuple[int, str, float, str]) -> str:
    """Fixture with the fan replaced by listings (id, title, price, store)."""
    page = promobit_with(offerLikes=40)
    data_start = page.index('application/json">') + len('application/json">')
    data_end = page.index("</script>", data_start)
    data = json.loads(page[data_start:data_end])
    offers = data["props"]["pageProps"]["serverOffers"]["offers"]
    fan = next(o for o in offers if o["offerId"] == 3242903)
    offers.remove(fan)
    for oid, title, price, store in variants:
        offers.append(dict(fan, offerId=oid, offerTitle=title, offerSlug=f"x-{oid}",
                           offerPrice=price, offerOldPrice=price * 3, storeName=store))
    return page[:data_start] + json.dumps(data) + page[data_end:]


MOUSE_X1 = (1, "Mouse Gamer com Fio Attack Shark X1, Ultraleve, 12.800 DPI, 6 Botões - Branco", 59.99, "Amazon")
MOUSE_X3P = (2, "Mouse Gamer com Fio Attack Shark X3, 12.800 DPI, 6 Botões Programáveis - Preto", 59.99, "Amazon")
MOUSE_X3B = (3, "Mouse Gamer com Fio Attack Shark X3, 12.800 DPI, 6 Botões Programáveis - Branco", 59.99, "Amazon")


def test_colour_variants_fold_into_one_message(cfg):
    other_store = (4, MOUSE_X1[1].replace("Branco", "Preto"), 59.99, "KaBuM!")
    other_price = (5, "Mouse Gamer com Fio Attack Shark X5, 26.000 DPI, 6 Botões Programáveis - Preto", 99.9, "Amazon")
    routes = dict(ROUTES_OK)
    routes["https://www.promobit.com.br/promocoes/recentes/"] = html(
        promobit_variants(MOUSE_X1, MOUSE_X3P, MOUSE_X3B, other_store, other_price))
    db, tg = Storage(":memory:"), FakeTelegram()
    rep = run(cfg, client_for(routes), db, tg, max_notifications=10)
    assert sorted(o.raw.source_id for o in rep.sent) == ["1", "4", "5"]
    (_, d), = [(o, d) for o, d in rep.evaluated if o.raw.source_id == "1"]
    assert [t.rsplit(" - ", 1)[1] for t in d.variants] == ["Preto", "Branco"]


def test_variant_of_recently_sent_is_not_sent_later(cfg):
    routes = dict(ROUTES_OK)
    routes["https://www.promobit.com.br/promocoes/recentes/"] = html(promobit_variants(MOUSE_X1))
    db, tg = Storage(":memory:"), FakeTelegram()
    run(cfg, client_for(routes), db, tg)
    routes["https://www.promobit.com.br/promocoes/recentes/"] = html(promobit_variants(MOUSE_X1, MOUSE_X3P))
    rep = run(cfg, client_for(routes), db, tg)
    assert rep.sent == [] and [o.raw.source_id for o in tg.offers] == ["1"]


# -- Telegram photo ------------------------------------------------------------
from ofertas.models import Decision, RawOffer, StoredOffer  # noqa: E402
from ofertas.notify import Telegram  # noqa: E402


def tg_with(responses: dict) -> tuple[Telegram, list]:
    calls = []

    def handler(req):
        method = req.url.path.rsplit("/", 1)[1]
        calls.append((method, json.loads(req.content)))
        return httpx.Response(200, json=responses[method])

    return Telegram("TOKEN", "42", transport=httpx.MockTransport(handler)), calls


def stored(image):
    raw = RawOffer(source="kabum", source_id="1", title="Mouse", price=59.99,
                   url="https://loja/x", store="KaBuM!", image=image)
    return StoredOffer(id=1, raw=raw, url_canon="https://loja/x", product_key="mouse",
                       is_new=True, price_changed=False)


OK = {"ok": True, "result": {"message_id": 7}}


def test_offer_with_image_goes_as_photo_with_caption_and_button():
    tg, calls = tg_with({"sendPhoto": OK})
    msg = tg.send_offer(stored("https://img/x.jpg"), Decision(ok=True))
    assert msg.message_id == 7 and msg.photo and "Mouse" in msg.text
    (method, body), = calls
    assert method == "sendPhoto" and body["photo"] == "https://img/x.jpg"
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert "Mouse" in body["caption"] and keyboard[0][0]["url"] == "https://loja/x"
    assert [b["callback_data"] for b in keyboard[1]] == ["fb:1:1", "fb:1:-1"]


def test_photo_rejected_falls_back_to_text():
    tg, calls = tg_with({"sendPhoto": {"ok": False, "description": "wrong type of the web page content"},
                         "sendMessage": OK})
    msg = tg.send_offer(stored("https://img/x.webp"), Decision(ok=True))
    assert msg.message_id == 7 and not msg.photo
    assert [m for m, _ in calls] == ["sendPhoto", "sendMessage"]


def test_no_image_sends_text():
    tg, calls = tg_with({"sendMessage": OK})
    tg.send_offer(stored(None), Decision(ok=True))
    assert [m for m, _ in calls] == ["sendMessage"]


# -- rotation and per-page failures --------------------------------------------
from ofertas.sources.base import ROTATION_SLOT_SECONDS, rotation  # noqa: E402
from ofertas.sources.promobit import PromobitSource  # noqa: E402


def test_rotation_covers_every_url_round_robin():
    urls = [f"u{i}" for i in range(7)]
    seen = []
    for slot in range(3):
        seen += rotation(urls, 3, now=slot * ROTATION_SLOT_SECONDS)
    assert set(seen) == set(urls)
    assert rotation(urls, 3, now=0) == ["u0", "u1", "u2"]
    assert rotation(urls, 10) == urls and rotation([], 3) == []


def test_one_bad_page_does_not_lose_the_others():
    page = fixture_text("promobit_recentes.html")
    c = client_for({"https://p.com/a": html(page), "https://p.com/b": httpx.Response(404)})
    assert len(PromobitSource(c, ["https://p.com/a", "https://p.com/b"]).fetch()) == 12
    with pytest.raises(Exception):
        PromobitSource(c, ["https://p.com/b"]).fetch()  # every page failed


def test_blocked_page_stops_the_source():
    page = fixture_text("promobit_recentes.html")
    calls = []
    c = client_for({"https://p.com/a": httpx.Response(403), "https://p.com/b": html(page)}, calls)
    with pytest.raises(BlockedError):
        PromobitSource(c, ["https://p.com/a", "https://p.com/b"]).fetch()
    assert "https://p.com/b" not in calls
