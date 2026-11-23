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
