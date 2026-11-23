"""One collection/notification cycle."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from ofertas import criteria
from ofertas.config import Config
from ofertas.bot import enabled_categories
from ofertas.bot import min_score as bot_min_score
from ofertas.dedupe import is_duplicate
from ofertas.http import BlockedError, PoliteClient
from ofertas.models import Decision, RawOffer, StoredOffer
from ofertas.normalize import product_key
from ofertas.notify import format_offer
from ofertas.sources import REGISTRY
from ofertas.storage import Storage, utcnow
from ofertas.triage import Triage

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
    enabled = enabled_categories(cfg, storage)  # /categorias overrides config

    need = [(off, d) for off, d in pre if d.ok and storage.get_verdict(off.id, off.raw.price) is None]
    if need and triage is not None:
        fresh = triage.judge([o for o, _ in need], stats={o.id: d.stats for o, d in need})
        with _tx(storage):
            for off, _ in need:
                if off.id in fresh:
                    storage.save_verdict(off.id, off.raw.price, fresh[off.id])
    out = []
    for off, d in pre:
        if not d.ok:
            out.append((off, d))
            continue
        v = storage.get_verdict(off.id, off.raw.price)
        out.append((off, criteria.with_verdict(d, v, min_score, enabled)))
    return out


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
    with _tx(storage):
        stored = [storage.upsert_offer(r, now) for r in raws]

    # Every offer currently listed is judged each run: the objective phase is
    # local and cheap, dedupe stops repeats, and verdicts are cached, so an
    # offer that heats up in the community later still gets its chance.
    ccfg, scfg = cfg.get("criteria"), cfg.get("stores")
    pre: list[tuple[StoredOffer, Decision]] = []
    seen_keys: set[str] = set()
    since = now - timedelta(days=ccfg.get("history_days", 90))
    for off in stored:
        dup, why = is_duplicate(
            storage, off, ccfg["resend_window_hours"], ccfg["resend_min_drop"], now
        )
        if not dup and off.product_key in seen_keys:
            dup, why = True, "repetida em outra fonte nesta execução"
        history = storage.price_history(off.product_key, since, before=now)
        d = criteria.objective(off.raw, ccfg, scfg, duplicate_reason=why, history=history)
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
    sent_names: set[str] = set()
    for off, decision in passing:
        if len(report.sent) >= cap:
            break
        # Same product posted several times with different titles: Claude's
        # normalised name catches what the title-based product_key misses.
        name = product_key(decision.verdict.name_norm) if decision.verdict else off.product_key
        if name in sent_names:
            continue
        sent_names.add(name)
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
