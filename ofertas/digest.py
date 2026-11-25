"""Daily summary ("🌙 Resumo do dia"): what was sent, Claude usage and the
best offers that almost passed, so the minimum score can be tuned with data.
Sent once a day by the runner after `digest.hour` (local time), and on demand
with /resumo."""

from __future__ import annotations

import html
from datetime import datetime, time, timezone

from ofertas.notify import brl


def local_midnight(now: datetime) -> datetime:
    local = now.astimezone()
    return datetime.combine(local.date(), time.min, tzinfo=local.tzinfo).astimezone(timezone.utc)


def digest_text(db, min_score: int, calls: int, call_limit: int, now: datetime,
                band: int = 20, max_items: int = 5) -> str:
    since = local_midnight(now)
    sent = db.sent_count(since)
    lines = [
        "🌙 <b>Resumo do dia</b>",
        f"📨 Enviadas hoje: {sent}",
        f"🤖 Claude: {calls}/{call_limit} chamadas",
        "",
    ]
    misses = db.near_misses(since, max(0, min_score - band), min_score, max_items)
    if misses:
        lines.append(f"<b>Quase passaram</b> (nota mínima {min_score}):")
        for r in misses:
            name = html.escape((r["name_norm"] or r["title"])[:70])
            store = html.escape(r["store"] or "?")
            lines.append(f"• <a href=\"{html.escape(r['url'], quote=True)}\">{name}</a> — "
                         f"{brl(r['price'])} @ {store} · nota {r['score']}")
        lines.append("")
        lines.append(f"Se gostou de alguma, /minimo {misses[0]['score']} deixaria passar ofertas assim.")
    else:
        lines.append(f"Nenhuma oferta ficou perto da nota mínima ({min_score}) hoje.")
    return "\n".join(lines)
