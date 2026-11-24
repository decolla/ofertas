"""Comparajogos: board-game price comparator. Its public home page ships an
Apollo cache in __NEXT_DATA__ with `price_reduction`: the games whose lowest
new price (across Brazilian stores) dropped the most, with the previous price.

The previous price is Comparajogos' own tracked price, not a store's "de"
price, so the declared discount here is a real history signal. The store is
whichever is cheapest at the moment, so we don't name one.
robots.txt allows the home; we never touch /api/ or /f/ (forum) paths.
"""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.sources.base import Source

BASE = "https://www.comparajogos.com.br"
STORE = "Várias lojas (menor preço no Comparajogos)"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class ComparajogosSource(Source):
    name = "comparajogos"

    @staticmethod
    def parse(page: str) -> list[RawOffer]:
        m = _NEXT_DATA.search(page)
        if not m:
            raise ValueError("Comparajogos: __NEXT_DATA__ não encontrado (layout mudou?)")
        state = (json.loads(m.group(1)).get("props", {}).get("pageProps", {})
                 .get("__APOLLO_STATE__") or {})
        root = state.get("ROOT_QUERY") or {}
        lists = [v for k, v in root.items() if k.startswith("price_reduction")]
        if not lists:
            raise ValueError("Comparajogos: 'price_reduction' ausente (layout mudou?)")
        offers = []
        for entry in (e for lst in lists if isinstance(lst, list) for e in lst):
            o = _to_offer(entry, state)
            if o:
                offers.append(o)
        return offers


def _ref(state: dict, obj: dict | None) -> dict:
    if isinstance(obj, dict) and "__ref" in obj:
        return state.get(obj["__ref"]) or {}
    return obj or {}


def _to_offer(entry: dict, state: dict) -> RawOffer | None:
    price_obj = _ref(state, entry.get("price"))
    product = _ref(state, price_obj.get("product"))
    price = price_obj.get("min_price_new")
    old = entry.get("old_price")
    if not product.get("slug") or not product.get("name") or not price or not price_obj.get("available"):
        return None
    wish = (product.get("count") or {}).get("wish")
    kind = {"expansion": "expansão", "rpg": "RPG"}.get(product.get("type"), "jogo de tabuleiro")
    return RawOffer(
        source="comparajogos",
        source_id=str(product["id"]),
        title=f"{product['name']} ({kind})",
        price=round(float(price), 2),
        url=f"{BASE}/item/{product['slug']}",
        store=STORE,
        list_price=round(float(old), 2) if old and old > price else None,
        image=product.get("image_url") or product.get("thumbnail_url"),
        extra={
            "tags": [t for t in (
                "jogo de tabuleiro", "preço anterior = histórico do Comparajogos",
                f"{price_obj.get('new_count')} lojas com estoque" if price_obj.get("new_count") else None,
                f"BGG {product['bgg_rating']:.1f}" if product.get("bgg_rating") else None,
                f"{wish} na lista de desejos" if wish else None,
            ) if t],
        },
    )
