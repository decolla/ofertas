"""Source registry. To add a source: create a module with a Source subclass,
register it here and add a block under `sources:` in config.yaml."""

from ofertas.sources.base import Source
from ofertas.sources.comparajogos import ComparajogosSource
from ofertas.sources.gatry import GatrySource
from ofertas.sources.kabum import KabumSource
from ofertas.sources.ofertaesperta import OfertaEspertaSource
from ofertas.sources.pechinchou import PechinchouSource
from ofertas.sources.pelando import PelandoSource
from ofertas.sources.promobit import PromobitSource
from ofertas.sources.vtex import OlympikusSource, ReservaSource

REGISTRY: dict[str, type[Source]] = {
    PromobitSource.name: PromobitSource,
    GatrySource.name: GatrySource,
    PelandoSource.name: PelandoSource,
    KabumSource.name: KabumSource,
    PechinchouSource.name: PechinchouSource,
    OfertaEspertaSource.name: OfertaEspertaSource,
    ComparajogosSource.name: ComparajogosSource,
    OlympikusSource.name: OlympikusSource,
    ReservaSource.name: ReservaSource,
}

__all__ = ["REGISTRY", "Source"]
