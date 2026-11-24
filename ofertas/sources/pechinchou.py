"""Pechinchou: community deal aggregator (all categories). Public pages
(/recentes, home) are Next.js SSR; the list is in pageProps.promos.results.

robots.txt only disallows /api/* and profile settings; we read the pages.
"""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.normalize import parse_brl
from ofertas.sources.base import Source

BASE = "https://www.pechinchou.com.br"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class PechinchouSource(Source):
    name = "pechinchou"

    @staticmethod
    def parse(page: str) -> list[RawOffer]:
        m = _NEXT_DATA.search(page)
        if not m:
            raise ValueError("Pechinchou: __NEXT_DATA__ não encontrado (layout mudou?)")
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        promos = props.get("promos")
        if not isinstance(promos, dict) or "results" not in promos:
            raise ValueError("Pechinchou: lista 'promos' ausente (layout mudou?)")
        return [o for o in (_to_offer(it) for it in promos["results"]) if o]


def _to_offer(it: dict) -> RawOffer | None:
    if it.get("status") != "ACTIVE" or it.get("is_any_text_price"):
        return None  # expired, or "price" is free text ("a partir de...")
    price = parse_brl(it.get("price"))
    if not price or price <= 1 or not it.get("title") or not it.get("id"):
        return None  # R$ 1 "first purchase" bait and incomplete posts
    old = parse_brl(it.get("old_price"))
    store = ((it.get("store") or {}).get("name") or "").strip() or None
    freight = it.get("freight") or ""
    coupons = [c for c in it.get("coupons") or [] if isinstance(c, str)]
    sub = it.get("subcategory") or {}
    tags = [t for t in (
        (sub.get("category") or {}).get("name"), sub.get("name"),
        "menor preço (Pechinchou)" if it.get("lowest_price") else None,
        freight or None,
    ) if t]
    return RawOffer(
        source="pechinchou",
        source_id=str(it["id"]),
        title=" ".join(it["title"].split()),
        price=price,
        url=f"{BASE}/oferta/{it['id']}",
        store=store,
        list_price=old if old and old > price else None,
        coupon=coupons[0] if coupons else None,
        # "Frete Grátis assinantes Meli+/Prime" is conditional: not counted.
        free_shipping=True if freight.lower().startswith("frete grátis todo") else None,
        votes=it.get("tot_likes_and_dislikes"),
        image=it.get("image"),
        published_at=it.get("created_at"),
        extra={"comments": it.get("total_comment"), "tags": tags},
    )
