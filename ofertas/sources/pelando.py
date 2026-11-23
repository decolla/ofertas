"""Pelando: /recentes is an Astro page; the deal list is in the public `props`
attribute of the RecentsFeedContent island, in Astro's serialised format
where every value is wrapped as [type, value] (type 1 = array)."""

from __future__ import annotations

import html
import json
import re

from ofertas.models import RawOffer
from ofertas.sources.base import Source

BASE = "https://www.pelando.com.br"
_ISLAND = re.compile(r'<astro-island[^>]*\sprops="(\{&quot;initialDeals[^"]*)"', re.S)


def astro_decode(v):
    """[0, x] -> x ; [1, [...]] -> list ; applied recursively inside dicts."""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        kind, value = v
        if kind == 1 and isinstance(value, list):
            return [astro_decode(i) for i in value]
        return astro_decode(value) if isinstance(value, dict) else value
    if isinstance(v, dict):
        return {k: astro_decode(x) for k, x in v.items()}
    return v


class PelandoSource(Source):
    name = "pelando"

    @staticmethod
    def parse(page: str) -> list[RawOffer]:
        m = _ISLAND.search(page)
        if not m:
            raise ValueError("Pelando: lista de ofertas não encontrada (layout mudou?)")
        props = json.loads(html.unescape(m.group(1)))
        deals = astro_decode(props["initialDeals"])
        offers = []
        for d in deals:
            offer = _to_offer(d)
            if offer:
                offers.append(offer)
        return offers


def _to_offer(d: dict) -> RawOffer | None:
    if d.get("kind") != "promotion" or d.get("status") != "active":
        return None  # coupons, expired deals
    price = d.get("price")
    if not price or price <= 1 or not d.get("title") or not d.get("slug"):
        return None
    store = (d.get("store") or {}).get("name")
    return RawOffer(
        source="pelando",
        source_id=str(d["id"]),
        title=" ".join(d["title"].split()),
        price=round(float(price), 2),
        url=f"{BASE}/d/{d['slug']}",
        store=store.strip() if store else None,
        free_shipping=True if d.get("freeShipping") else None,
        temperature=d.get("temperature"),
        image=d.get("imageUrl"),
        published_at=d.get("createdAt"),
        extra={
            "comments": d.get("commentCount"),
            "cashback": d.get("cashback"),
            "source_url": d.get("sourceUrl"),
        },
    )
