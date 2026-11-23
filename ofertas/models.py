from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawOffer:
    """An offer exactly as collected from a source, before any judgement."""

    source: str
    source_id: str
    title: str
    price: float
    url: str
    store: str | None = None
    list_price: float | None = None  # declared "de" price
    coupon: str | None = None
    free_shipping: bool | None = None
    votes: int | None = None  # likes / upvotes
    temperature: float | None = None  # engagement / heat score
    image: str | None = None
    published_at: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def declared_discount(self) -> float | None:
        if self.list_price and self.list_price > self.price > 0:
            return 1 - self.price / self.list_price
        return None


@dataclass
class StoredOffer:
    """A RawOffer after being persisted, with derived keys."""

    id: int
    raw: RawOffer
    url_canon: str
    product_key: str
    is_new: bool
    price_changed: bool
    category: str | None = None


@dataclass
class Verdict:
    """Claude's judgement of one offer at one price."""

    score: int
    fake: bool
    flags: list[str]
    category: str
    name_norm: str
    condition: str
    reason: str
    model: str = "haiku"


@dataclass
class PriceStats:
    """Current price against our own recorded history for the product."""

    points: int
    median: float | None = None
    minimum: float | None = None
    discount_vs_median: float | None = None
    is_new_low: bool = False


@dataclass
class Decision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    insufficient_history: bool = False
    verdict: Verdict | None = None
    pending: bool = False  # waiting for a Claude verdict
    stats: PriceStats | None = None
