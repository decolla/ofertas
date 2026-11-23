import pytest

from ofertas.sources.gatry import GatrySource
from ofertas.sources.promobit import PromobitSource
from tests.conftest import fixture_text


class TestPromobit:
    def test_parses_offers_from_next_data(self):
        offers = PromobitSource.parse(fixture_text("promobit_recentes.html"))
        assert len(offers) == 12
        fan = next(o for o in offers if o.source_id == "3242903")
        assert fan.title.startswith("Mondial Ventilador de mesa 30cm")
        assert fan.price == 71.92
        assert fan.list_price == 277.25
        assert fan.store == "Amazon"
        assert fan.free_shipping is True
        assert fan.url == "https://www.promobit.com.br/oferta/" + fan.url.split("/oferta/")[1]
        assert fan.declared_discount == pytest.approx(1 - 71.92 / 277.25)

    def test_offer_without_old_price_has_no_list_price(self):
        offers = PromobitSource.parse(fixture_text("promobit_recentes.html"))
        assert all(o.list_price is None or o.list_price > o.price for o in offers)

    def test_skips_coupon_only_and_editorial_posts(self):
        html = fixture_text("promobit_recentes.html").replace('"offerPrice": 71.92', '"offerPrice": 0.01')
        ids = {o.source_id for o in PromobitSource.parse(html)}
        assert "3242903" not in ids

    def test_missing_next_data_raises(self):
        with pytest.raises(ValueError, match="__NEXT_DATA__"):
            PromobitSource.parse("<html><body>nada</body></html>")


class TestGatry:
    def test_parses_ssr_list(self):
        offers = GatrySource.parse(fixture_text("gatry_promocoes.html"))
        assert len(offers) == 40
        monitor = next(o for o in offers if o.source_id == "9db606fd-0e31-460c-97ff-164fc1bc80c3")
        assert monitor.title == 'Monitor Samsung 32" FHD,75 Hz,Freesync, HDMI, DP,Preto, S3'
        assert monitor.price == 899.0
        assert monitor.store == "Amazon"
        assert monitor.url.startswith("https://gatry.com/promocoes/9db606fd-")
        assert monitor.list_price is None

    def test_thousands_separator_and_missing_store(self):
        offers = {o.source_id: o for o in GatrySource.parse(fixture_text("gatry_promocoes.html"))}
        assert offers["63fa50d0-c873-49a7-90f4-80f12e0b0467"].price == 4199.0
        assert offers["7afe192e-be44-4f3e-83a8-d58a5f2e23fe"].store is None

    def test_layout_change_raises(self):
        with pytest.raises(ValueError, match="layout"):
            GatrySource.parse("<ul><li><span>algo novo</span></li></ul>")
