"""Oferta Esperta: community deal aggregator (all categories). The home page is
a Next.js App Router page: the offers ship in the React Server Components
payload (`self.__next_f.push([1, "..."])`) as an `initialOffers` array. Offer
links (/promocao/<id>/<slug>) are read from the page's own <a href>.

robots.txt disallows /api, /out/, /o/, /comprar/ and account pages: we read
only the public listing.
"""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.normalize import parse_brl
from ofertas.sources.base import Source

BASE = "https://www.ofertaesperta.com"
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_HREF = re.compile(r'href="(/promocao/([a-z0-9]+)/[^"]*)"')


def rsc_text(page: str) -> str:
    return "".join(json.loads(f'"{chunk}"') for chunk in _PUSH.findall(page))


def initial_offers(page: str) -> list[dict]:
    text = rsc_text(page)
    dec = json.JSONDecoder()
    out: list[dict] = []
    for m in re.finditer(r'"initialOffers":\s*', text):
        arr, _ = dec.raw_decode(text, m.end())
        out.extend(x for x in arr if isinstance(x, dict))
    return out


class OfertaEspertaSource(Source):
    name = "ofertaesperta"

    @staticmethod
    def parse(page: str) -> list[RawOffer]:
        items = initial_offers(page)
        if not items:
            raise ValueError("Oferta Esperta: 'initialOffers' não encontrado (layout mudou?)")
        links = {oid: href for href, oid in _HREF.findall(page)}
        return [o for o in (_to_offer(it, links) for it in items) if o]


def _val(v):
    """RSC encodes missing values as "$undefined" (and refs as "$..."): drop them."""
    return None if isinstance(v, str) and v.startswith("$") else v


def _to_offer(it: dict, links: dict[str, str]) -> RawOffer | None:
    it = {k: _val(v) for k, v in it.items()}
    if it.get("reported") or not it.get("id") or not it.get("title"):
        return None
    # priceNow "R$ 1.619" + priceCents ",10"
    price = parse_brl(f"{it.get('priceNow') or ''}{it.get('priceCents') or ''}")
    if not price or price <= 1:
        return None
    old = parse_brl(it.get("priceFrom"))
    href = links.get(it["id"])
    tags = [t for t in (
        it.get("category"), it.get("insight"),
        "Prime" if it.get("isPrime") else None,
        "Meli+" if it.get("isMeliPlus") else None,
        "Recorrente" if it.get("isSubscribeSave") else None,
        "marketplace" if it.get("isMarketplace") else None,
        f"regras do cupom: {it['couponRules']}" if it.get("couponRules") else None,
    ) if t]
    return RawOffer(
        source="ofertaesperta",
        source_id=it["id"],
        title=" ".join(it["title"].split()),
        price=price,
        url=BASE + href if href else BASE,
        store=(it.get("storeName") or "").strip() or None,
        list_price=old if old and old > price else None,
        coupon=it.get("couponCode") or None,
        free_shipping=True if it.get("freeShipping") else None,
        votes=it.get("likes"),
        image=it.get("image"),
        published_at=it.get("postedDate"),
        ended=bool(it.get("encerrada")),
        extra={"comments": it.get("comments"), "medal": it.get("medal"), "tags": tags},
    )
