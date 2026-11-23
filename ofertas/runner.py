"""One collection/notification cycle."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ofertas import criteria
from ofertas.config import Config
from ofertas.dedupe import is_duplicate
from ofertas.http import BlockedError, PoliteClient
from ofertas.models import Decision, RawOffer, StoredOffer
from ofertas.notify import format_offer
from ofertas.sources import REGISTRY
from ofertas.storage import Storage, utcnow

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send_text(self, text: str, button: tuple[str, str] | None = None) -> int: ...
    def send_offer(self, offer: StoredOffer, decision: Decision) -> int: ...


class ConsoleNotifier:
    """Used by --dry-run: prints what would be sent."""

    def __init__(self, out=print):
        self.out = out

    def send_text(self, text: str, button=None) -> int:
        self.out("\n" + _strip_html(text) + (f"\n[{button[0]}] {button[1]}" if button else ""))
        return 0

    def send_offer(self, offer: StoredOffer, decision: Decision) -> int:
        return self.send_text(format_offer(offer, decision), ("Abrir oferta", offer.raw.url))


def _strip_html(text: str) -> str:
    import html

    return html.unescape(re.sub(r"<[^>]+>", "", text))


@dataclass
class RunReport:
    collected: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    evaluated: list[tuple[StoredOffer, Decision]] = field(default_factory=list)
    sent: list[StoredOffer] = field(default_factory=list)


@contextmanager
def _tx(storage: Storage):
    """Transaction unless one is already open (dry-run wraps everything)."""
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
            got = cls(client, scfg["urls"]).fetch()
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


def run(
    cfg: Config,
    client: PoliteClient,
    storage: Storage,
    notifier: Notifier,
    dry_run: bool = False,
    only_source: str | None = None,
    max_notifications: int | None = None,
    now: datetime | None = None,
) -> RunReport:
    now = now or utcnow()
    report = RunReport()
    if dry_run:
        storage.begin()  # everything below is rolled back at the end
    try:
        raws = collect(cfg, client, storage, notifier, report, only_source)
        with _tx(storage):
            stored = [storage.upsert_offer(r, now) for r in raws]

        # Dry-run shows a verdict for everything collected; a real run only
        # judges what is new or changed price since the last run.
        candidates = stored if dry_run else [s for s in stored if s.is_new or s.price_changed]
        ccfg, scfg = cfg.get("criteria"), cfg.get("stores")
        passing: list[tuple[StoredOffer, Decision]] = []
        seen_keys: set[str] = set()
        for off in candidates:
            dup, why = is_duplicate(
                storage, off, ccfg["resend_window_hours"], ccfg["resend_min_drop"], now
            )
            if not dup and off.product_key in seen_keys:
                dup, why = True, "repetida em outra fonte nesta execução"
            decision = criteria.evaluate(off.raw, ccfg, scfg, duplicate_reason=why)
            report.evaluated.append((off, decision))
            if decision.ok:
                seen_keys.add(off.product_key)
                passing.append((off, decision))

        passing.sort(key=lambda p: p[0].raw.declared_discount or 0, reverse=True)
        cap = max_notifications or ccfg.get("max_notifications_per_run", 5)
        paused = storage.get_setting("paused") == "1"
        if paused and not dry_run:
            log.info("paused_skip_send", extra={"would_send": len(passing)})
            passing = []
        for off, decision in passing[:cap]:
            try:
                msg_id = notifier.send_offer(off, decision)
            except Exception as exc:
                log.error("send_failed", extra={"offer_id": off.id, "error": str(exc)})
                continue
            with _tx(storage):
                storage.mark_sent(off, msg_id, now)
            report.sent.append(off)
            log.info("offer_sent", extra={"offer_id": off.id, "title": off.raw.title,
                                          "price": off.raw.price, "source": off.raw.source,
                                          "dry_run": dry_run})
    finally:
        if dry_run:
            storage.rollback()
    log.info("run_done", extra={
        "collected": report.collected, "failed": list(report.failed),
        "evaluated": len(report.evaluated), "sent": len(report.sent), "dry_run": dry_run,
    })
    return report


def _esc(s: str) -> str:
    import html

    return html.escape(s)


def _safe_send(notifier: Notifier, text: str) -> None:
    try:
        notifier.send_text(text)
    except Exception as exc:
        log.error("alert_send_failed", extra={"error": str(exc)})
