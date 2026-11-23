"""KaBuM!: campaign pages (/ofertas/<slug>) are server-rendered with the
products in __NEXT_DATA__ (pageProps.dataOfferServer.data). Campaign slugs
change often, so they are discovered from links on the public home page.

robots.txt allows /ofertas/*; we never hit /busca, sorting params or APIs.
"""

from __future__ import annotations

import json
import re

from ofertas.models import RawOffer
from ofertas.sources.base import Source

BASE = "https://www.kabum.com.br"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_CAMPAIGN = re.compile(r'href="(/ofertas/[a-z0-9-]+)"')


def campaign_paths(home_html: str, limit: int) -> list[str]:
    seen: list[str] = []
    for path in _CAMPAIGN.findall(home_html):
        if path not in seen:
            seen.append(path)
    return seen[:limit]


class KabumSource(Source):
    name = "kabum"

    def __init__(self, client, urls, max_campaigns: int = 3):
        super().__init__(client, urls)
        self.max_campaigns = max_campaigns

    def fetch(self) -> list[RawOffer]:
        """urls[0] is the home page used for discovery; extra urls are fixed pages."""
        home, *fixed = self.urls
        resp = self.client.get(home)
        resp.raise_for_status()
        pages = [BASE + p for p in campaign_paths(resp.text, self.max_campaigns)] + fixed
        offers: dict[str, RawOffer] = {}
        for url in pages:
            r = self.client.get(url)
            if r.status_code == 404:
                continue  # campaign ended between discovery and fetch
            r.raise_for_status()
            for o in self.parse(r.text):
                offers.setdefault(o.source_id, o)
        return list(offers.values())

    @staticmethod
    def parse(page: str) -> list[RawOffer]:
        m = _NEXT_DATA.search(page)
        if not m:
            raise ValueError("KaBuM: __NEXT_DATA__ não encontrado (layout mudou?)")
        props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        items = (props.get("dataOfferServer") or {}).get("data") or []
        campaign = props.get("offerName")
        return [o for o in (_to_offer(it, campaign) for it in items) if o]


def _to_offer(it: dict, campaign: str | None) -> RawOffer | None:
    flags = it.get("flags") or {}
    if not it.get("available") or flags.get("openbox") or flags.get("isPreOrder"):
        return None  # out of stock, open box (not new) or pre-order
    pix = it.get("priceWithDiscount") or it.get("price")
    if not pix or not it.get("name") or not it.get("code"):
        return None
    seller = (it.get("sellerName") or "").strip()
    # Marketplace sellers are a different store for whitelist purposes.
    store = "KaBuM!" if seller in ("", "KaBuM!") else f"{seller} (marketplace KaBuM!)"
    old = it.get("oldPrice") or it.get("price")
    slug = it.get("friendlyName") or ""
    return RawOffer(
        source="kabum",
        source_id=str(it["code"]),
        title=" ".join(it["name"].split()),
        price=round(float(pix), 2),
        url=f"{BASE}/produto/{it['code']}/{slug}".rstrip("/"),
        store=store,
        list_price=round(float(old), 2) if old and old > pix else None,
        free_shipping=True if flags.get("freeShipping") else None,
        image=it.get("image"),
        extra={
            "tags": ["Preço no PIX"] + (["Prime Ninja"] if it.get("primePriceWithDiscount") else []),
            "category_store": it.get("category"),
            "manufacturer": it.get("manufacturer"),
            "stock": it.get("quantity"),
            "rating": it.get("averageRating"),
            "rating_count": it.get("ratingCount"),
            "campaign": campaign,
            "card_price": it.get("price"),
        },
    )
