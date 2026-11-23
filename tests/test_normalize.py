import pytest

from ofertas.normalize import canonical_url, normalize_store, parse_brl, product_key


@pytest.mark.parametrize(
    "text,expected",
    [
        ("R$ 1.149,00", 1149.0),
        ("R$ 45,64", 45.64),
        ("R$1.234.567,89", 1234567.89),
        ("899", 899.0),
        ("1.299", 1299.0),
        ("12.5", 12.5),
        (71.92, 71.92),
        ("", None),
        (None, None),
        ("grátis", None),
    ],
)
def test_parse_brl(text, expected):
    assert parse_brl(text) == expected


def test_product_key_is_order_and_accent_insensitive():
    a = product_key("SSD Kingston NV3 1TB, M.2 NVMe — Leitura 6000MB/s")
    b = product_key("Kingston NV3 SSD 1TB M.2 NVMe Leitura 6000mb/s")
    assert a == b


def test_product_key_distinguishes_capacity():
    assert product_key("SSD Kingston NV3 1TB") != product_key("SSD Kingston NV3 2TB")


def test_canonical_url_drops_tracking_and_fragment():
    u = "https://WWW.Amazon.com.br/dp/B0ABC/?utm_source=x&tag=afil-20&th=1#reviews"
    assert canonical_url(u) == "https://www.amazon.com.br/dp/B0ABC?th=1"


def test_canonical_url_trailing_slash():
    assert canonical_url("https://gatry.com/promocoes/abc/") == canonical_url("https://gatry.com/promocoes/abc")


def test_normalize_store():
    assert normalize_store("KaBuM!") == normalize_store("kabum") == "kabum"
    assert normalize_store("Mercado Livre") == "mercadolivre"
    assert normalize_store(None) is None
