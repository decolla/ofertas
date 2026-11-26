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


from ofertas.sources.kabum import KabumSource, campaign_paths  # noqa: E402
from ofertas.sources.pelando import PelandoSource, astro_decode  # noqa: E402


class TestPelando:
    def test_astro_decode(self):
        assert astro_decode([0, 5]) == 5
        assert astro_decode([1, [[0, "a"], [0, {"x": [0, 1]}]]]) == ["a", {"x": 1}]
        assert astro_decode({"s": [0, {"name": [0, "Amazon"]}]}) == {"s": {"name": "Amazon"}}

    def test_parses_promotions_skips_coupons(self):
        offers = PelandoSource.parse(fixture_text("pelando_recentes.html"))
        assert len(offers) == 10  # 12 deals in the fixture, 2 are coupons
        vonixx = offers[0]
        assert vonixx.title == "Vonixx RESTAURAX 500ML Proteção UV Toque Seco"
        assert vonixx.price == 36.26 and vonixx.store == "Amazon"
        assert vonixx.temperature == 5 and vonixx.list_price is None
        assert vonixx.url == "https://www.pelando.com.br/d/vonixx-restaurax-500ml-protecao-uv-toque-seco-1f1f"

    def test_community_page_has_deals_after_other_props(self):
        # /c/<slug> pages: the island props start with community/sort/feedName.
        page = fixture_text("pelando_recentes.html").replace(
            'props="{&quot;initialDeals',
            'props="{&quot;community&quot;:[0,{&quot;slug&quot;:[0,&quot;para-meu-lar&quot;]}],'
            '&quot;sort&quot;:[0,&quot;createdAt&quot;],&quot;initialDeals', 1)
        assert len(PelandoSource.parse(page)) == 10

    def test_missing_island_raises(self):
        with pytest.raises(ValueError, match="layout"):
            PelandoSource.parse("<html></html>")


class TestKabum:
    def test_campaign_discovery(self):
        paths = campaign_paths(fixture_text("kabum_home.html"), limit=5)
        assert paths == ["/ofertas/agoraoununca", "/ofertas/esquentablackdasblacks"]
        assert campaign_paths(fixture_text("kabum_home.html"), limit=1) == ["/ofertas/agoraoununca"]

    def test_parses_campaign_products(self):
        offers = KabumSource.parse(fixture_text("kabum_oferta.html"))
        assert len(offers) == 12
        psu = next(o for o in offers if o.source_id == "907603")
        assert psu.title.startswith("Fonte Gigabyte UD1000GM PG5 V2, 1000W")
        assert psu.price == 649.99 and psu.list_price == 1111.1
        assert psu.store == "KaBuM!" and psu.free_shipping is True
        assert psu.url.startswith("https://www.kabum.com.br/produto/907603/fonte-gigabyte")
        assert "Preço no PIX" in psu.extra["tags"]

    def test_skips_openbox_unavailable_and_labels_marketplace(self):
        page = fixture_text("kabum_oferta.html")
        page = page.replace('"openbox": false', '"openbox": true', 1)
        page = page.replace('"sellerName": "KaBuM!"', '"sellerName": "Loja X"', 2)
        offers = {o.source_id: o for o in KabumSource.parse(page)}
        assert "907603" not in offers  # first product is now open box
        assert offers["592292"].store == "Loja X (marketplace KaBuM!)"


from ofertas.sources.promobit import photo_url  # noqa: E402


@pytest.mark.parametrize("raw,url", [
    ("/static/p/1.jpg", "https://www.promobit.com.br/static/p/1.jpg"),
    ("https://www.promobit.com.br//static/p/2.png", "https://www.promobit.com.br/static/p/2.png"),
    ("/8279.png", "https://i.promobit.com.br/8279.png"),  # image CDN
    (None, None),
])
def test_promobit_photo_url(raw, url):
    assert photo_url(raw) == url


# -- stage 5: more aggregators ---------------------------------------------
from ofertas.sources.comparajogos import ComparajogosSource  # noqa: E402
from ofertas.sources.ofertaesperta import OfertaEspertaSource  # noqa: E402
from ofertas.sources.pechinchou import PechinchouSource  # noqa: E402


class TestPechinchou:
    def test_parses_next_data(self):
        offers = PechinchouSource.parse(fixture_text("pechinchou_recentes.html"))
        assert len(offers) == 9  # 10 in the fixture, one "R$ 1,00 primeira compra" bait
        phone = next(o for o in offers if o.source_id == "150178")
        assert phone.price == 1039.99 and phone.list_price == 1249.9
        assert phone.store == "Mercado Livre" and phone.coupon == "DESCONTOSMELI"
        assert phone.url == "https://www.pechinchou.com.br/oferta/150178"
        assert phone.free_shipping is True and phone.image.startswith("https://")
        assert "Telefonia" in phone.extra["tags"]

    def test_conditional_free_shipping_is_not_free(self):
        offers = {o.source_id: o for o in PechinchouSource.parse(fixture_text("pechinchou_recentes.html"))}
        assert offers["152389"].free_shipping is None  # "Frete Grátis assinantes Meli+"
        assert "menor preço (Pechinchou)" in offers["152389"].extra["tags"]

    def test_layout_change_raises(self):
        with pytest.raises(ValueError, match="layout"):
            PechinchouSource.parse('<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>')


class TestOfertaEsperta:
    def test_parses_rsc_payload(self):
        offers = OfertaEspertaSource.parse(fixture_text("ofertaesperta_home.html"))
        assert len(offers) == 10
        phone = offers[0]
        assert phone.price == 1619.10 and phone.list_price == 2699.0
        assert phone.store == "Mercado Livre" and phone.coupon == "TECH2411"
        assert phone.url.startswith("https://www.ofertaesperta.com/promocao/cmwup5vgc002olj0eo55qrp4k/")
        assert any(t.startswith("regras do cupom: Compra mínima") for t in phone.extra["tags"])

    def test_rsc_undefined_is_none(self):
        offers = {o.source_id: o for o in OfertaEspertaSource.parse(fixture_text("ofertaesperta_home.html"))}
        blender = offers["cmwup4p6q001dlj0e4uqqpzij"]
        assert blender.coupon is None  # "$undefined" in the payload
        assert not any("$undefined" in t for t in blender.extra["tags"])

    def test_layout_change_raises(self):
        with pytest.raises(ValueError, match="layout"):
            OfertaEspertaSource.parse("<html></html>")


class TestComparajogos:
    def test_parses_price_reductions(self):
        offers = ComparajogosSource.parse(fixture_text("comparajogos_home.html"))
        assert len(offers) == 15
        gwt = next(o for o in offers if o.source_id == "55011")
        assert gwt.title == "Great Western Trail: Second Edition (jogo de tabuleiro)"
        assert gwt.price == 557.07 and gwt.list_price == 911.11
        assert gwt.url == "https://www.comparajogos.com.br/item/great-western-trail-second-edition"
        assert gwt.votes is None  # wishlist count is not a vote on the deal

    def test_layout_change_raises(self):
        with pytest.raises(ValueError, match="layout"):
            ComparajogosSource.parse('<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>')


from ofertas.sources.vtex import OlympikusSource, ReservaSource  # noqa: E402


class TestVtex:
    def test_store_theme_state(self):
        offers = OlympikusSource.parse(fixture_text("olympikus_blackfriday.html"))
        assert [o.source_id for o in offers] == ["9529", "8282"]  # 11532 is out of stock
        voa = offers[0]
        assert voa.title == "Tênis Olympikus Voa 2" and voa.store == "Olympikus"
        assert (voa.price, voa.list_price) == (199.99, 329.99)
        assert voa.url == "https://www.olympikus.com.br/tenis-olympikus-voa-2-unissex-43533261-3-030/p"
        assert voa.image.startswith("https://olympikus.vtexassets.com/")
        assert "marca: Olympikus" in voa.extra["tags"] and voa.votes is None

    def test_faststore_groups_skus_and_drops_out_of_stock(self):
        offers = ReservaSource.parse(fixture_text("reserva_ofertas.html"))
        assert [o.source_id for o in offers] == ["8988", "128911", "132927"]
        camiseta = offers[0]
        assert camiseta.price == 99.0 and camiseta.list_price == 129.0  # cheapest size kept
        assert camiseta.title == "Camiseta Básica Brasa Pica-pau Bordado Reserva"  # brand appended
        assert camiseta.url.startswith("https://www.usereserva.com/camiseta-basica-brasa")
        assert camiseta.url.endswith("/p") and camiseta.store == "Reserva"

    def test_unknown_layout_raises(self):
        with pytest.raises(ValueError, match="layout"):
            ReservaSource.parse("<html><body>nada</body></html>")
