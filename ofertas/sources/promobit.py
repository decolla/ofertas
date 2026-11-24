"""Promobit: offers come in the page's public __NEXT_DATA__ JSON."""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.sources.base import Source

BASE = "https://www.promobit.com.br"
IMAGE_CDN = "https://i.promobit.com.br"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_OFFER_LISTS = ("serverOffers", "serverFeaturedOffers")


class PromobitSource(Source):
    name = "promobit"

    @staticmethod
    def parse(html: str) -> list[RawOffer]:
        m = _NEXT_DATA.search(html)
        if not m:
            raise ValueError("Promobit: __NEXT_DATA__ não encontrado (layout mudou?)")
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        items: list[dict] = []
        for key in _OFFER_LISTS:
            block = props.get(key)
            if isinstance(block, dict):
                block = block.get("offers")
            if isinstance(block, list):
                items.extend(block)

        offers: dict[str, RawOffer] = {}
        for it in items:
            offer = _to_offer(it)
            if offer:
                offers.setdefault(offer.source_id, offer)
        return list(offers.values())


def photo_url(photo: str | None) -> str | None:
    """Promobit publishes '/<id>.jpg' (served by its image CDN, as the site
    does) or '/static/p/<id>.jpg' / a full URL (sometimes with '//static')."""
    if not photo:
        return None
    if photo.startswith(BASE):
        photo = photo[len(BASE):]
    if photo.startswith("http"):
        return photo
    photo = "/" + photo.lstrip("/")
    return (BASE if photo.startswith("/static/") else IMAGE_CDN) + photo


def _to_offer(it: dict) -> RawOffer | None:
    price = it.get("offerPrice")
    store = (it.get("storeName") or "").strip() or None
    if not it.get("offerId") or not it.get("offerTitle") or not price:
        return None
    # Coupon-only posts and Promobit's own editorial posts carry symbolic prices.
    if price <= 1 or (store or "").lower() == "promobit":
        return None
    tags = [t.get("name", "") for t in it.get("offerTags") or []]
    old = it.get("offerOldPrice") or None
    photo = it.get("offerPhoto")
    return RawOffer(
        source="promobit",
        source_id=str(it["offerId"]),
        title=it["offerTitle"].strip(),
        price=round(float(price), 2),
        url=f"{BASE}/oferta/{it['offerSlug']}/" if it.get("offerSlug") else BASE,
        store=store,
        list_price=round(float(old), 2) if old and old > price else None,
        coupon=it.get("offerCoupon") or None,
        free_shipping=any("frete gr" in t.lower() for t in tags),
        votes=it.get("offerLikes"),
        temperature=it.get("offerEngagementScore"),
        image=photo_url(photo),
        published_at=it.get("offerPublished"),
        extra={
            "status": it.get("offerStatusName"),
            "comments": it.get("offerComments"),
            "ratings": it.get("ratings"),
            "tags": tags,
            "store_domain": it.get("storeDomain"),
            "category_id": it.get("categoryId"),
        },
    )
