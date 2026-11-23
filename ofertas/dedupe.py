"""Was this product already sent recently? Pure rule + storage lookup."""

from __future__ import annotations

from datetime import datetime, timedelta

from ofertas.models import StoredOffer
from ofertas.storage import Storage


def should_resend(new_price: float, last_sent_price: float | None, min_drop: float) -> bool:
    """Allowed when never sent in the window, or the price dropped by >= min_drop."""
    if last_sent_price is None:
        return True
    return new_price <= last_sent_price * (1 - min_drop)


def is_duplicate(
    storage: Storage,
    offer: StoredOffer,
    window_hours: float,
    min_drop: float,
    now: datetime,
) -> tuple[bool, str | None]:
    row = storage.last_sent(offer.product_key, offer.url_canon, now - timedelta(hours=window_hours))
    if row is None:
        return False, None
    if should_resend(offer.raw.price, row["price"], min_drop):
        return False, None
    return True, f"já enviada em {row['sent_at'][:16]} por R$ {row['price']:.2f}"
