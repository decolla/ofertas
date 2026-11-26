"""One collection/notification cycle."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from ofertas import criteria, prefs
from ofertas.config import Config
from ofertas.bot import enabled_categories
from ofertas.bot import min_score as bot_min_score
from ofertas.dedupe import is_duplicate
from ofertas.digest import digest_text
from ofertas.http import BlockedError, PoliteClient
from ofertas.models import Decision, RawOffer, StoredOffer
from ofertas.normalize import normalize_store, product_key, same_family
from ofertas.notify import SentMessage, format_offer
from ofertas.sources import REGISTRY
from ofertas.storage import Storage, utcnow
from ofertas.triage import Triage, feedback_payload

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send_text(self, text: str, button: tuple[str, str] | None = None) -> int: ...
    def send_offer(self, offer: StoredOffer, decision: Decision) -> SentMessage: ...
    def close_offer(self, message_id: int, text: str, photo: bool, url: str, offer_id: int) -> None: ...


class ConsoleNotifier:
    """Used by --dry-run: prints what would be sent."""

    def __init__(self, out=print):
        self.out = out

    def send_text(self, text: str, button=None) -> int:
        self.out("\n" + _strip_html(text) + (f"\n[{button[0]}] {button[1]}" if button else ""))
        return 0

    def send_offer(self, offer: StoredOffer, decision: Decision) -> SentMessage:
        text = format_offer(offer, decision)
        self.send_text(text, ("Abrir oferta", offer.raw.url))
        return SentMessage(0, text, photo=False)

    def close_offer(self, message_id, text, photo, url, offer_id) -> None:
        self.out(f"\n[encerrada] mensagem {message_id}: {url}")


def _strip_html(text: str) -> str:
    import html

    return html.unescape(re.sub(r"<[^>]+>", "", text))


@dataclass
class RunReport:
    collected: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    evaluated: list[tuple[StoredOffer, Decision]] = field(default_factory=list)
    sent: list[StoredOffer] = field(default_factory=list)
    closed: int = 0  # sent messages marked as ended this run


@contextmanager
def _tx(storage: Storage):
    """Transaction unless one is already open."""
    if storage.conn.in_transaction:
        yield
        return
    storage.begin()
    try:
        yield
    except BaseException:
        storage.rollback()
        raise
    storage.commit()


def collect(
    cfg: Config, client: PoliteClient, storage: Storage, notifier: Notifier,
    report: RunReport, only_source: str | None,
) -> list[RawOffer]:
    raws: list[RawOffer] = []
    threshold = cfg.get("health.alert_after_failures", 5)
    for name, scfg in (cfg.get("sources") or {}).items():
        if only_source and name != only_source:
            continue
        if not scfg.get("enabled", True) and not only_source:
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            log.error("unknown_source", extra={"source": name})
            continue
        try:
            opts = {k: v for k, v in scfg.items() if k not in ("enabled", "urls")}
            opts.setdefault("max_seconds", cfg.get("http.source_time_budget_seconds", 240))
            got = cls(client, scfg["urls"], **opts).fetch()
        except Exception as exc:  # isolate: one broken source never stops the others
            kind = "blocked" if isinstance(exc, BlockedError) else "error"
            log.warning("source_failed", extra={"source": name, "kind": kind, "error": str(exc)})
            report.failed[name] = str(exc)
            with _tx(storage):
                n, alerted = storage.source_failed(name, f"{kind}: {exc}")
                if n >= threshold and not alerted:
                    _safe_send(notifier, (
                        f"⚠️ A fonte <b>{name}</b> falhou {n} vezes seguidas.\n"
                        f"Último erro: <code>{_esc(str(exc)[:300])}</code>"
                    ))
                    storage.source_alerted(name)
            continue
        log.info("source_ok", extra={"source": name, "offers": len(got)})
        report.collected[name] = len(got)
        with _tx(storage):
            if storage.source_ok(name):
                _safe_send(notifier, f"✅ A fonte <b>{name}</b> voltou a funcionar.")
        raws.extend(got)
    return raws


def judge(
    cfg: Config, storage: Storage, triage: Triage | None,
    pre: list[tuple[StoredOffer, Decision]],
) -> list[tuple[StoredOffer, Decision]]:
    """Phase 2: attach Claude verdicts (cached per offer+price) to objective passes."""
    if not cfg.get("claude.enabled", False):
        return pre
    min_score = bot_min_score(cfg, storage)  # /minimo overrides config
    wish_score = min(min_score, int(cfg.get("criteria.wishlist.min_score", 65)))
    enabled = enabled_categories(cfg, storage)  # /categorias overrides config
    ccfg = cfg.get("criteria")
    fb = criteria_fallback(cfg)

    need = [(off, d) for off, d in pre if d.ok and storage.get_verdict(off.id, off.raw.price) is None]
    # Budget goes first where a good verdict is likeliest: the user's /quero
    # terms, favourite brands, then real price history, then community signal, then the
    # declared discount (stores inflate it: KaBuM! is mostly fake at 40-59%).
    need.sort(key=lambda p: (
        p[1].wish is not None, p[1].brand is not None, not p[1].insufficient_history,
        criteria.community_ok(p[0].raw, fb), p[0].raw.declared_discount or 0,
    ), reverse=True)
    if need and triage is not None:
        n_fb = int(cfg.get("claude.feedback_examples", 20))
        feedback = feedback_payload(storage.recent_feedback(n_fb)) if n_fb else None
        fresh = triage.judge([o for o, _ in need], stats={o.id: d.stats for o, d in need},
                             feedback=feedback)
        with _tx(storage):
            for off, _ in need:
                if off.id in fresh:
                    storage.save_verdict(off.id, off.raw.price, fresh[off.id])
    # Subscription limit hit: offers backed by our own price history (not
    # just a store's "de" price) still go out, flagged; the rest wait.
    unavailable = (triage is not None and triage.rate_limited
                   and cfg.get("claude.send_history_when_unavailable", True))
    out = []
    for off, d in pre:
        if not d.ok:
            out.append((off, d))
            continue
        v = storage.get_verdict(off.id, off.raw.price)
        if v is None and unavailable and not d.insufficient_history:
            out.append((off, Decision(
                ok=True, reasons=d.reasons + ["sem Claude (limite de uso): só o histórico de preços"],
                stats=d.stats, wish=d.wish, brand=d.brand, unjudged=True)))
            continue
        need_score = min_score if v is None else criteria.required_score(
            min_score, v.category, d.brand is not None, ccfg,
            brand_offset=int(cfg.get("brands.score_offset", 0)),
            wish_score=wish_score if d.wish else None)
        out.append((off, criteria.with_verdict(d, v, need_score, enabled)))
    return out


def criteria_fallback(cfg: Config) -> dict:
    return cfg.get("criteria")["fallback"]


def close_ended(cfg: Config, storage: Storage, notifier: Notifier, ended: list[RawOffer],
                now: datetime) -> int:
    """Edits messages we sent for deals the source now lists as ended."""
    keys = {(r.source, r.source_id) for r in ended}
    since = now - timedelta(days=cfg.get("ended.window_days", 7))
    closed = 0
    for row in storage.open_sent_for(keys, since):
        try:
            notifier.close_offer(row["message_id"], row["text"], bool(row["photo"]),
                                 row["url"], row["offer_id"])
        except Exception as exc:  # message too old/deleted: don't retry forever
            log.warning("close_failed", extra={"sent_id": row["id"], "error": str(exc)})
        with _tx(storage):
            storage.mark_ended(row["id"], now)
        closed += 1
        log.info("offer_closed", extra={"offer_id": row["offer_id"], "url": row["url"]})
    return closed


def maybe_digest(cfg: Config, storage: Storage, notifier: Notifier, now: datetime) -> bool:
    """Once a day, after digest.hour local time."""
    if not cfg.get("digest.enabled", True) or storage.get_setting("paused") == "1":
        return False
    local = now.astimezone()
    if local.hour < int(cfg.get("digest.hour", 21)):
        return False
    day = local.date().isoformat()
    if storage.get_setting("digest_day") == day:
        return False
    with _tx(storage):
        storage.set_setting("digest_day", day)
    _safe_send(notifier, digest_text(
        storage, bot_min_score(cfg, storage), storage.claude_calls(local.date()),
        cfg.get("claude.daily_call_limit", 100), now,
        band=cfg.get("digest.near_miss_band", 20), max_items=cfg.get("digest.max_items", 5),
    ))
    return True


def run(
    cfg: Config,
    client: PoliteClient,
    storage: Storage,
    notifier: Notifier,
    triage: Triage | None = None,
    dry_run: bool = False,
    only_source: str | None = None,
    max_notifications: int | None = None,
    now: datetime | None = None,
) -> RunReport:
    """One cycle. For --dry-run pass a Storage.snapshot(): nothing persists."""
    now = now or utcnow()
    report = RunReport()
    raws = collect(cfg, client, storage, notifier, report, only_source)
    ended = [r for r in raws if r.ended]
    raws = [r for r in raws if not r.ended]
    report.closed = close_ended(cfg, storage, notifier, ended, now)
    with _tx(storage):
        stored = [storage.upsert_offer(r, now) for r in raws]

    # Every offer currently listed is judged each run: the objective phase is
    # local and cheap, dedupe stops repeats, and verdicts are cached, so an
    # offer that heats up in the community later still gets its chance.
    ccfg, scfg = cfg.get("criteria"), cfg.get("stores")
    pre: list[tuple[StoredOffer, Decision]] = []
    seen_keys: set[str] = set()
    since = now - timedelta(days=ccfg.get("history_days", 90))
    wishes, muted = prefs.get_terms(storage, prefs.WISHLIST), prefs.get_terms(storage, prefs.MUTED)
    wcfg = criteria.wish_cfg(ccfg, ccfg.get("wishlist") or {})
    brands = list(cfg.get("brands.favorites") or [])
    for off in stored:
        dup, why = is_duplicate(
            storage, off, ccfg["resend_window_hours"], ccfg["resend_min_drop"], now
        )
        if not dup and off.product_key in seen_keys:
            dup, why = True, "repetida em outra fonte nesta execução"
        history = storage.price_history(off.product_key, since, before=now)
        mute = prefs.first_match(muted, off.raw.title)
        wish = None if mute else prefs.first_match(wishes, off.raw.title)
        d = criteria.objective(off.raw, wcfg if wish else ccfg, scfg,
                               duplicate_reason=why, history=history)
        d.wish = wish
        d.brand = prefs.first_match(brands, f"{off.raw.title} {off.raw.store or ''}")
        if mute:
            d.ok = False
            d.reasons.append(f"silenciado: {mute}")
        if d.ok:
            seen_keys.add(off.product_key)
        pre.append((off, d))

    report.evaluated = judge(cfg, storage, triage, pre)
    passing = [(o, d) for o, d in report.evaluated if d.ok]
    passing.sort(
        key=lambda p: (p[1].verdict.score if p[1].verdict else 0, p[0].raw.declared_discount or 0),
        reverse=True,
    )
    cap = max_notifications or ccfg.get("max_notifications_per_run", 5)
    if storage.get_setting("paused") == "1" and not dry_run:
        log.info("paused_skip_send", extra={"would_send": len(passing)})
        passing = []
    window = now - timedelta(hours=ccfg["resend_window_hours"])
    family = [(r["product_key"], r["store_norm"], r["price"]) for r in storage.recent_sent(window)]
    sent_before: dict[str, float] = {}  # product_key(name_norm) -> lowest price sent in the window
    for r in storage.recent_sent_names(window):
        k = product_key(r["name_norm"])
        sent_before[k] = min(r["price"], sent_before.get(k, r["price"]))
    min_drop = ccfg["resend_min_drop"]
    heads: list[tuple[StoredOffer, Decision]] = []
    sent_names: set[str] = set()
    for off, decision in passing:
        # Same product posted several times with different titles: Claude's
        # normalised name catches what the title-based product_key misses.
        name = product_key(decision.verdict.name_norm) if decision.verdict else off.product_key
        if name in sent_names:
            continue
        # ...and across runs: a re-post of something already sent (new offer
        # id, other title) waits for the same price-drop rule as the dedupe.
        if name in sent_before and off.raw.price > sent_before[name] * (1 - min_drop):
            log.info("name_dup_skip", extra={"offer_id": off.id, "name": name})
            continue
        # Colour/model variants of one listing (KaBuM! lists each colour as a
        # product): fold into the first one, here or sent in the dedupe window.
        head = next((h for h in heads if _variant_of(off, h[0])), None)
        if head is not None:
            head[1].variants.append(off.raw.title)
            continue
        if any(_variant_row(off, *row) for row in family):
            log.info("variant_skip", extra={"offer_id": off.id, "title": off.raw.title})
            continue
        sent_names.add(name)
        heads.append((off, decision))

    for off, decision in heads:
        if len(report.sent) >= cap:
            break
        try:
            msg = notifier.send_offer(off, decision)
        except Exception as exc:
            log.error("send_failed", extra={"offer_id": off.id, "error": str(exc)})
            continue
        with _tx(storage):
            storage.mark_sent(off, msg.message_id, now, text=msg.text, photo=msg.photo)
        report.sent.append(off)
        log.info("offer_sent", extra={"offer_id": off.id, "title": off.raw.title,
                                      "price": off.raw.price, "source": off.raw.source,
                                      "dry_run": dry_run})
    if not dry_run and not only_source:
        maybe_digest(cfg, storage, notifier, now)
    log.info("run_done", extra={
        "collected": report.collected, "failed": list(report.failed),
        "evaluated": len(report.evaluated), "sent": len(report.sent),
        "closed": report.closed, "ended_seen": len(ended), "dry_run": dry_run,
    })
    return report


VARIANT_PRICE_TOLERANCE = 0.03


def _variant_row(off: StoredOffer, pkey: str, store_norm: str | None, price: float) -> bool:
    if not store_norm or normalize_store(off.raw.store) != store_norm or pkey == off.product_key:
        return False  # identical keys are the regular dedupe's job (price-drop rule)
    if abs(off.raw.price - price) > price * VARIANT_PRICE_TOLERANCE:
        return False
    return same_family(off.product_key, pkey)


def _variant_of(off: StoredOffer, other: StoredOffer) -> bool:
    return _variant_row(off, other.product_key, normalize_store(other.raw.store), other.raw.price)


def _esc(s: str) -> str:
    import html

    return html.escape(s)


def _safe_send(notifier: Notifier, text: str) -> None:
    try:
        notifier.send_text(text)
    except Exception as exc:
        log.error("alert_send_failed", extra={"error": str(exc)})
