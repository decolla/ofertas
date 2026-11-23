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
        self.offers.append(offer)
        return 100 + len(self.offers)


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
    db, out = Storage(":memory:"), []
    rep = run(cfg, client_for(ROUTES_OK), db, ConsoleNotifier(out.append), dry_run=True)
    assert len(rep.sent) == 1 and "Mondial Ventilador" in out[0] and "<b>" not in out[0]
    assert db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 0


def test_paused_collects_but_does_not_send(cfg):
    db, tg = Storage(":memory:"), FakeTelegram()
    db.set_setting("paused", "1")
    run(cfg, client_for(ROUTES_OK), db, tg)
    assert tg.offers == []
    assert db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 52
