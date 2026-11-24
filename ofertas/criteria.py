"""Objective notification criterion. Pure functions: no I/O, fully testable.

Two phases, so Claude is only asked about offers worth asking about:
  1. objective(): store, dedupe and price signal: with enough history, price
     vs. median or a new lowest price; without it, the declared discount
     (alone if large, otherwise backed by community votes).
  2. with_verdict(): Claude's score, fake flags, condition and category.
"""

from __future__ import annotations

import statistics

from ofertas.models import Decision, PriceStats, RawOffer, Verdict
from ofertas.normalize import normalize_store


def store_allowed(store: str | None, stores_cfg: dict) -> bool:
    norm = normalize_store(store)
    if stores_cfg["mode"] == "whitelist":
        allowed = {normalize_store(s) for s in stores_cfg.get("whitelist") or []}
        return norm in allowed
    blocked = {normalize_store(s) for s in stores_cfg.get("blacklist") or []}
    return norm not in blocked


def community_cfg(offer: RawOffer, fallback_cfg: dict) -> dict:
    """Global community thresholds with per-source overrides (scales differ)."""
    return {**fallback_cfg, **(fallback_cfg.get("per_source") or {}).get(offer.source, {})}


def community_ok(offer: RawOffer, fallback_cfg: dict) -> bool:
    fb = community_cfg(offer, fallback_cfg)
    votes_ok = (
        offer.votes is not None and fb.get("min_votes") is not None
        and offer.votes >= fb["min_votes"]
    )
    temp_ok = (
        offer.temperature is not None and fb.get("min_temperature") is not None
        and offer.temperature >= fb["min_temperature"]
    )
    return votes_ok or temp_ok


def community_alone(offer: RawOffer, fallback_cfg: dict) -> bool:
    """A post so hot in the community that it deserves Claude's look on its own."""
    alone = community_cfg(offer, fallback_cfg).get("min_temperature_alone")
    return alone is not None and offer.temperature is not None and offer.temperature >= alone


def price_stats(price: float, history: list[float], new_low_margin: float = 0.01) -> PriceStats:
    """Stats of the current price against prior observations (pure)."""
    if not history:
        return PriceStats(points=0)
    med = statistics.median(history)
    low = min(history)
    return PriceStats(
        points=len(history),
        median=round(med, 2),
        minimum=round(low, 2),
        discount_vs_median=round(1 - price / med, 4) if med > 0 else None,
        is_new_low=price <= low * (1 - new_low_margin),
    )


def objective(
    offer: RawOffer,
    criteria_cfg: dict,
    stores_cfg: dict,
    duplicate_reason: str | None = None,
    history: list[float] | None = None,
) -> Decision:
    reasons: list[str] = []
    ok = True

    if not store_allowed(offer.store, stores_cfg):
        ok = False
        reasons.append(f"loja fora da lista: {offer.store or 'desconhecida'}")

    if duplicate_reason:
        ok = False
        reasons.append(duplicate_reason)

    stats = price_stats(offer.price, history or [], criteria_cfg.get("min_new_low_margin", 0.01))
    enough = stats.points >= criteria_cfg.get("min_history_points", 5)
    if enough:
        need = criteria_cfg.get("min_discount_vs_median", 0.25)
        disc = stats.discount_vs_median or 0
        if disc >= need:
            reasons.append(f"{disc:.0%} abaixo da mediana (R$ {stats.median:.2f}, {stats.points} pontos)")
        elif stats.is_new_low:
            reasons.append(f"menor preço já registrado (antes R$ {stats.minimum:.2f})")
        else:
            ok = False
            reasons.append(f"só {disc:.0%} abaixo da mediana R$ {stats.median:.2f} (precisa {need:.0%})")
    else:
        ok = _fallback(offer, criteria_cfg["fallback"], reasons, stats) and ok

    return Decision(ok=ok, reasons=reasons, insufficient_history=not enough, stats=stats)


def _fallback(offer: RawOffer, fb: dict, reasons: list[str], stats: PriceStats) -> bool:
    """No usable history: declared discount, alone if big, else with community."""
    reasons.append(f"histórico insuficiente ({stats.points} pontos)")
    disc = offer.declared_discount
    community = community_ok(offer, fb)
    src = community_cfg(offer, fb)  # discount thresholds can be per source too
    alone = src.get("min_declared_discount_alone")
    if disc is not None and alone is not None and disc >= alone:
        reasons.append(f"desconto declarado {disc:.0%}")
        return True
    if community_alone(offer, fb):
        reasons.append(f"comunidade muito quente: {offer.temperature:.0f}°")
        return True
    if disc is None or disc < src["min_declared_discount"]:
        shown = f"{disc:.0%}" if disc is not None else "sem preço 'de'"
        reasons.append(f"desconto declarado insuficiente ({shown})")
        return False
    reasons.append(f"desconto declarado {disc:.0%}")
    if community:
        reasons.append(f"comunidade: {offer.votes or 0} likes, engajamento {offer.temperature or 0:.0f}")
        return True
    reasons.append("sem sinal da comunidade")
    return False


def with_verdict(
    decision: Decision,
    verdict: Verdict | None,
    min_score: int,
    enabled_categories: list[str] | None,
) -> Decision:
    """Applies Claude's verdict on top of an objective decision (pure)."""
    reasons = list(decision.reasons)
    if verdict is None:
        return Decision(ok=False, reasons=reasons + ["aguardando avaliação do Claude"],
                        insufficient_history=decision.insufficient_history, pending=decision.ok,
                        stats=decision.stats)
    ok = decision.ok
    if verdict.fake:
        ok = False
        reasons.append(f"desconto falso ({', '.join(verdict.flags) or 'sem detalhe'})")
    if verdict.condition in ("usado", "recondicionado"):
        ok = False
        reasons.append(f"produto {verdict.condition}")
    if enabled_categories is not None and verdict.category not in enabled_categories:
        ok = False
        reasons.append(f"categoria desativada: {verdict.category}")
    if verdict.score < min_score:
        ok = False
        reasons.append(f"nota {verdict.score} < {min_score}")
    else:
        reasons.append(f"nota {verdict.score}")
    return Decision(ok=ok, reasons=reasons, insufficient_history=decision.insufficient_history,
                    verdict=verdict, stats=decision.stats)
