import pytest

from ofertas.criteria import objective as evaluate, store_allowed
from ofertas.models import RawOffer

CRIT = {"fallback": {"min_declared_discount": 0.30, "min_votes": 15, "min_temperature": 300}}
WL = {"mode": "whitelist", "whitelist": ["Amazon", "KaBuM!"], "blacklist": []}
BL = {"mode": "blacklist", "whitelist": [], "blacklist": ["Shopee", "AliExpress"]}


def offer(**kw):
    base = dict(source="promobit", source_id="1", title="X", price=60.0, url="https://x",
                store="Amazon", list_price=100.0, votes=20, temperature=50)
    base.update(kw)
    return RawOffer(**base)


@pytest.mark.parametrize(
    "store,cfg,expected",
    [("Amazon", WL, True), ("Kabum", WL, True), ("Shopee", WL, False), (None, WL, False),
     ("Shopee", BL, False), ("Amazon", BL, True), (None, BL, True)],
)
def test_store_allowed(store, cfg, expected):
    assert store_allowed(store, cfg) is expected


def test_passes_with_discount_and_votes():
    d = evaluate(offer(), CRIT, WL)
    assert d.ok and d.insufficient_history


def test_passes_with_temperature_instead_of_votes():
    assert evaluate(offer(votes=0, temperature=400), CRIT, WL).ok


@pytest.mark.parametrize(
    "kw,why",
    [
        ({"list_price": 80.0}, "desconto declarado insuficiente (25%)"),
        ({"list_price": None}, "sem preço 'de'"),
        ({"votes": 3, "temperature": 10}, "sem sinal da comunidade"),
        ({"store": "Shopee"}, "loja fora da lista: Shopee"),
    ],
)
def test_rejections_explain_why(kw, why):
    d = evaluate(offer(**kw), CRIT, WL)
    assert not d.ok
    assert any(why in r for r in d.reasons)


def test_duplicate_blocks():
    d = evaluate(offer(), CRIT, WL, duplicate_reason="já enviada")
    assert not d.ok and "já enviada" in d.reasons


# -- stage 3: price history ------------------------------------------------
from ofertas.criteria import price_stats  # noqa: E402

HCRIT = {
    "min_discount_vs_median": 0.25, "min_history_points": 5, "min_new_low_margin": 0.01,
    "fallback": {"min_declared_discount_alone": 0.40, "min_declared_discount": 0.30,
                 "min_votes": 15, "min_temperature": 300},
}
HIST = [100, 102, 98, 100, 101]  # median 100, min 98


def test_price_stats():
    s = price_stats(70, HIST)
    assert (s.points, s.median, s.minimum) == (5, 100, 98)
    assert s.discount_vs_median == 0.30 and s.is_new_low
    assert price_stats(98, HIST).is_new_low is False  # equal to the minimum is not a new low
    assert price_stats(10, []).points == 0


@pytest.mark.parametrize("price,ok,why", [
    (75, True, "25% abaixo da mediana"),
    (96, True, "menor preço já registrado"),
    (98, False, "só 2% abaixo da mediana"),
    (120, False, "precisa 25%"),
])
def test_history_rule(price, ok, why):
    d = evaluate(offer(price=price, list_price=None, votes=0, temperature=0), HCRIT, WL, history=HIST)
    assert d.ok is ok and not d.insufficient_history
    assert any(why in r for r in d.reasons), d.reasons


def test_history_overrides_inflated_declared_discount():
    # Declares 70% off, but it's the usual price we keep seeing: no.
    d = evaluate(offer(price=100, list_price=333, votes=50), HCRIT, WL, history=HIST)
    assert not d.ok


def test_too_few_points_falls_back_and_is_flagged():
    d = evaluate(offer(price=55, list_price=100, votes=0, temperature=0), HCRIT, WL, history=[100, 100])
    assert d.ok and d.insufficient_history
    assert "histórico insuficiente (2 pontos)" in d.reasons


@pytest.mark.parametrize("kw,ok", [
    ({"price": 60, "votes": 0, "temperature": 0}, True),   # 40% alone is enough
    ({"price": 65, "votes": 0, "temperature": 0}, False),  # 35% needs community
    ({"price": 65, "votes": 20}, True),                    # 35% + community
    ({"price": 75, "votes": 500}, False),                  # 25%: never in fallback
])
def test_fallback_without_history(kw, ok):
    assert evaluate(offer(list_price=100, **kw), HCRIT, WL, history=[]).ok is ok


PCRIT = {**HCRIT, "fallback": {**HCRIT["fallback"], "per_source": {
    "pelando": {"min_votes": None, "min_temperature": 150, "min_temperature_alone": 400}}}}


@pytest.mark.parametrize("temp,list_price,ok", [
    (450, None, True),    # very hot, no "de" price: goes on to Claude
    (200, None, False),   # warm but no declared discount
    (200, 100, True),     # 35% + community (pelando scale)
    (120, 100, False),    # 35% but not hot enough for pelando
])
def test_pelando_community_scale(temp, list_price, ok):
    o = offer(source="pelando", price=65, list_price=list_price, votes=None, temperature=temp)
    assert evaluate(o, PCRIT, WL, history=[]).ok is ok


def test_per_source_override_does_not_leak():
    # promobit keeps the global 300 engagement scale
    o = offer(price=65, list_price=100, votes=0, temperature=200)
    assert evaluate(o, PCRIT, WL, history=[]).ok is False


def test_per_source_discount_alone():
    """Comparajogos' old price is real history: 25% alone is enough there."""
    crit = {**HCRIT, "fallback": {**HCRIT["fallback"], "per_source": {
        "comparajogos": {"min_declared_discount_alone": 0.25}}}}
    cj = offer(source="comparajogos", price=70, list_price=100, votes=None, temperature=None)
    assert evaluate(cj, crit, WL | {"mode": "blacklist"}, history=[]).ok
    pm = offer(price=70, list_price=100, votes=0, temperature=0)
    assert not evaluate(pm, crit, WL | {"mode": "blacklist"}, history=[]).ok
