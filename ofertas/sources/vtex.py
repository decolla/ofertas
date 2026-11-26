"""Direct VTEX stores: the store's own public sale pages (Black Friday,
outlet). VTEX has two storefronts and both ship the product list in the page:

- store-theme (render-runtime): `<template data-varname="__STATE__">` holds
  an Apollo-like cache; products are `Product:sp-<id>-none` with a
  `priceRange` (sellingPrice/listPrice) and items -> sellers -> offer stock.
- FastStore (Next.js): `__NEXT_DATA__` -> pageProps.data.search.products
  .edges[].node, schema.org-like (`offers.offers[].price/listPrice`). Each
  node is one SKU (size/colour); they are grouped by `productGroupID`.

No community votes and the "de" price is the store's own: the declared
discount alone rarely means much, so these offers lean on our price history
and on Claude. Stores are added only when robots.txt allows the page and the
terms of use don't forbid automated reading (checked 2026-11-25; Havaianas
excluded by its terms, Hering/Insider render products in the browser only).
"""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.sources.base import Source

_STATE = re.compile(
    r'<template data-type="json" data-varname="__STATE__"[^>]*>\s*<script>(.*?)</script>', re.S)
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_PRODUCT_KEY = re.compile(r"Product:sp-(\d+)-none")


class VtexSource(Source):
    """Subclasses set name, BASE (https://www.store.com.br) and STORE."""

    BASE = ""
    STORE = ""

    @classmethod
    def parse(cls, page: str) -> list[RawOffer]:  # type: ignore[override]
        offers = parse_render_state(page, cls.name, cls.BASE, cls.STORE)
        if offers is None:
            offers = parse_faststore(page, cls.name, cls.BASE, cls.STORE)
        if offers is None:
            raise ValueError(f"{cls.STORE}: lista de produtos VTEX não encontrada (layout mudou?)")
        return offers


# -- store-theme (__STATE__) ----------------------------------------------------
def parse_render_state(page: str, source: str, base: str, store: str) -> list[RawOffer] | None:
    m = _STATE.search(page)
    if not m:
        return None
    state = json.loads(m.group(1))
    keys = [k for k in state if _PRODUCT_KEY.fullmatch(k)]
    if not keys:
        return None
    out = []
    for k in keys:
        o = _state_offer(state, state[k], source, base, store)
        if o:
            out.append(o)
    return out


def _ref(state: dict, obj):
    if isinstance(obj, dict) and obj.get("type") == "id" and "id" in obj:
        return state.get(obj["id"]) or {}
    return obj if isinstance(obj, dict) else {}


def _items(p: dict) -> list:
    for k, v in p.items():
        if k.startswith("items") and isinstance(v, list):
            return v
    return []


def _state_offer(state: dict, p: dict, source: str, base: str, store: str) -> RawOffer | None:
    name, link = p.get("productName"), p.get("link")
    if not name or not link or not p.get("productId"):
        return None
    pr = _ref(state, p.get("priceRange"))
    selling, listing = _ref(state, pr.get("sellingPrice")), _ref(state, pr.get("listPrice"))
    price, old = selling.get("lowPrice"), listing.get("lowPrice")
    if not price or price <= 1:
        return None
    in_stock, image = False, None
    for item_ref in _items(p):
        item = _ref(state, item_ref)
        if image is None:
            imgs = item.get("images") or []
            image = _ref(state, imgs[0]).get("imageUrl") if imgs else None
        for seller_ref in item.get("sellers") or []:
            offer = _ref(state, _ref(state, seller_ref).get("commertialOffer"))
            if (offer.get("AvailableQuantity") or 0) > 0:
                in_stock = True
    if not in_stock:
        return None
    cats = (p.get("categories") or {}).get("json") or []
    return RawOffer(
        source=source,
        source_id=str(p["productId"]),
        title=" ".join(name.split()),
        price=round(float(price), 2),
        url=base + link,
        store=store,
        list_price=round(float(old), 2) if old and old > price else None,
        image=image,
        extra={"tags": [t for t in (
            f"marca: {p.get('brand')}" if p.get("brand") else None,
            cats[0].strip("/").replace("/", " > ") if cats else None,
            "loja oficial (preço 'de' é da própria loja)",
        ) if t]},
    )


# -- FastStore (__NEXT_DATA__) --------------------------------------------------
def parse_faststore(page: str, source: str, base: str, store: str) -> list[RawOffer] | None:
    m = _NEXT_DATA.search(page)
    if not m:
        return None
    data = (json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("data") or {})
    edges = ((data.get("search") or {}).get("products") or {}).get("edges")
    if not isinstance(edges, list):
        return None
    best: dict[str, RawOffer] = {}  # productGroupID -> cheapest in-stock SKU
    for e in edges:
        o = _faststore_offer((e or {}).get("node") or {}, source, base, store)
        if o and (o.source_id not in best or o.price < best[o.source_id].price):
            best[o.source_id] = o
    return list(best.values())


def _faststore_offer(n: dict, source: str, base: str, store: str) -> RawOffer | None:
    group = n.get("isVariantOf") or {}
    name, slug = group.get("name") or n.get("name"), n.get("slug")
    gid = group.get("productGroupID") or n.get("sku")
    offers = [o for o in (n.get("offers") or {}).get("offers") or []
              if str(o.get("availability", "")).endswith("InStock")]
    if not name or not slug or not gid or not offers:
        return None
    o = min(offers, key=lambda x: x.get("price") or 1e12)
    price, old = o.get("price"), o.get("listPrice")
    if not price or price <= 1:
        return None
    brand = (n.get("brand") or {}).get("name")
    title = " ".join(name.split())
    if brand and brand.lower() not in title.lower():
        title = f"{title} {brand.title()}"  # Claude and brand matching need it
    images = n.get("image") or []
    return RawOffer(
        source=source,
        source_id=str(gid),
        title=title,
        price=round(float(price), 2),
        url=f"{base}/{slug}/p",
        store=store,
        list_price=round(float(old), 2) if old and old > price else None,
        image=images[0].get("url") if images else None,
        extra={"tags": [t for t in (
            f"marca: {brand}" if brand else None,
            "loja oficial (preço 'de' é da própria loja)",
        ) if t]},
    )


class OlympikusSource(VtexSource):
    name = "olympikus"
    BASE = "https://www.olympikus.com.br"
    STORE = "Olympikus"


class ReservaSource(VtexSource):
    name = "reserva"
    BASE = "https://www.usereserva.com"
    STORE = "Reserva"
