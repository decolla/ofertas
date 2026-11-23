"""Objective notification criterion. Pure functions: no I/O, fully testable.

Stage 1 (current): no price history and no Claude yet, so every offer goes
through the "insufficient history" path: declared discount AND community
signal. Stages 2-3 add the Claude score and the median/lowest-price rule.
"""

from __future__ import annotations

from ofertas.models import Decision, RawOffer
from ofertas.normalize import normalize_store


def store_allowed(store: str | None, stores_cfg: dict) -> bool:
    norm = normalize_store(store)
    if stores_cfg["mode"] == "whitelist":
        allowed = {normalize_store(s) for s in stores_cfg.get("whitelist") or []}
        return norm in allowed
    blocked = {normalize_store(s) for s in stores_cfg.get("blacklist") or []}
    return norm not in blocked


def community_ok(offer: RawOffer, fallback_cfg: dict) -> bool:
    votes_ok = offer.votes is not None and offer.votes >= fallback_cfg["min_votes"]
    temp_ok = (
        offer.temperature is not None and offer.temperature >= fallback_cfg["min_temperature"]
    )
    return votes_ok or temp_ok


def evaluate(
    offer: RawOffer,
    criteria_cfg: dict,
    stores_cfg: dict,
    duplicate_reason: str | None = None,
) -> Decision:
    reasons: list[str] = []
    ok = True

    if not store_allowed(offer.store, stores_cfg):
        ok = False
        reasons.append(f"loja fora da lista: {offer.store or 'desconhecida'}")

    if duplicate_reason:
        ok = False
        reasons.append(duplicate_reason)

    fb = criteria_cfg["fallback"]
    disc = offer.declared_discount
    if disc is None or disc < fb["min_declared_discount"]:
        ok = False
        shown = f"{disc:.0%}" if disc is not None else "sem preço 'de'"
        reasons.append(f"desconto declarado insuficiente ({shown})")
    else:
        reasons.append(f"desconto declarado {disc:.0%}")

    if community_ok(offer, fb):
        reasons.append(f"comunidade: {offer.votes or 0} likes, engajamento {offer.temperature or 0:.0f}")
    else:
        ok = False
        reasons.append("sem sinal da comunidade")

    return Decision(ok=ok, reasons=reasons, insufficient_history=True)
