"""Source registry. To add a source: create a module with a Source subclass,
register it here and add a block under `sources:` in config.yaml."""

from ofertas.sources.base import Source
from ofertas.sources.gatry import GatrySource
from ofertas.sources.kabum import KabumSource
from ofertas.sources.pelando import PelandoSource
from ofertas.sources.promobit import PromobitSource

REGISTRY: dict[str, type[Source]] = {
    PromobitSource.name: PromobitSource,
    GatrySource.name: GatrySource,
    PelandoSource.name: PelandoSource,
    KabumSource.name: KabumSource,
}

__all__ = ["REGISTRY", "Source"]
