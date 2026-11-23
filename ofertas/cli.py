from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from datetime import date
from pathlib import Path

from ofertas.config import ConfigError, load_config
from ofertas.http import PoliteClient
from ofertas.logs import setup_logging
from ofertas.notify import Telegram, brl
from ofertas.runner import ConsoleNotifier, RunReport, run
from ofertas.storage import Storage
from ofertas.triage import ClaudeCLI, Triage

log = logging.getLogger("ofertas")


def _client(cfg) -> PoliteClient:
    return PoliteClient(
        user_agent=cfg.user_agent,
        timeout=cfg.get("http.timeout_seconds", 20),
        retries=cfg.get("http.retries", 3),
        backoff_base=cfg.get("http.backoff_base_seconds", 2),
        min_interval=cfg.get("http.min_interval_per_host_seconds", 5),
    )


def _telegram(cfg) -> Telegram:
    if not cfg.telegram_token or not cfg.telegram_chat_id:
        raise ConfigError("TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID precisam estar no .env")
    return Telegram(cfg.telegram_token, cfg.telegram_chat_id)


def _print_report(report: RunReport) -> None:
    print("\n=== Coleta ===")
    for name, n in report.collected.items():
        print(f"  {name}: {n} ofertas")
    for name, err in report.failed.items():
        print(f"  {name}: FALHOU — {err}")
    print(f"\n=== Avaliação ({len(report.evaluated)} ofertas) ===")
    for off, d in sorted(report.evaluated, key=lambda p: (not p[1].ok, not p[1].pending)):
        mark = "✅" if d.ok else ("⏳" if d.pending else "❌")
        r = off.raw
        print(f"{mark} [{r.source}] {r.title[:60]} — {brl(r.price)} @ {r.store}")
        print(f"     {'; '.join(d.reasons)}")
    print(f"\n=== {len(report.sent)} mensagem(ns) {'simulada(s)' if report.sent else ''} ===")


def _triage(cfg, usage: Storage, notifier) -> Triage | None:
    if not cfg.get("claude.enabled", False):
        return None
    model = cfg.get("claude.model", "haiku")
    cli = ClaudeCLI(
        command=cfg.get("claude.command", "claude"),
        model=model,
        timeout=cfg.get("claude.timeout_seconds", 180),
        cwd=cfg.root / "data" / "claude-cwd",  # empty dir: no project CLAUDE.md
    )

    def alert(text: str) -> None:
        try:
            notifier.send_text(text)
        except Exception as exc:
            log.error("alert_send_failed", extra={"error": str(exc)})

    return Triage(cli, usage, daily_limit=cfg.get("claude.daily_call_limit", 100),
                  batch_size=cfg.get("claude.batch_size", 20), model=model, on_limit=alert)


def cmd_run(cfg, args) -> int:
    lock_path = cfg.path("lock_file")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("already_running")
            return 0
        notifier = ConsoleNotifier() if args.dry_run else _telegram(cfg)
        real = Storage(cfg.path("database"))
        # Dry-run works on an in-memory copy; Claude usage is still counted on
        # the real DB so repeated dry-runs can't blow the daily budget.
        work = real.snapshot() if args.dry_run else real
        triage = None
        if not args.dry_run or args.with_claude:
            triage = _triage(cfg, real, notifier)
        try:
            with _client(cfg) as client:
                report = run(cfg, client, work, notifier, triage=triage, dry_run=args.dry_run,
                             only_source=args.source, max_notifications=args.limit)
        finally:
            if work is not real:
                work.close()
            real.close()
        if args.dry_run:
            _print_report(report)
        return 0


def cmd_status(cfg, args) -> int:
    db = Storage(cfg.path("database"))
    today = date.today()
    used = db.claude_calls(today)
    print(f"Claude hoje: {used}/{cfg.get('claude.daily_call_limit', 100)} chamadas")
    print(f"Pausado: {'sim' if db.get_setting('paused') == '1' else 'não'}")
    for row in db.conn.execute("SELECT * FROM source_health ORDER BY source"):
        print(f"Fonte {row['source']}: {row['consecutive_failures']} falha(s) seguidas, "
              f"último ok {row['last_ok'] or '-'}")
    n = db.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    sent = db.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
    print(f"Ofertas no banco: {n} · enviadas: {sent}")
    db.close()
    return 0


def cmd_test_telegram(cfg, args) -> int:
    tg = _telegram(cfg)
    tg.send_text("🤖 Teste do monitor de ofertas: tudo certo por aqui!",
                 ("Abrir Telegram", "https://telegram.org"))
    print("Mensagem de teste enviada.")
    return 0


def cmd_init_db(cfg, args) -> int:
    Storage(cfg.path("database")).close()
    print(f"Banco pronto em {cfg.path('database')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ofertas", description="Monitor de ofertas → Telegram")
    p.add_argument("--config", type=Path, default=None, help="caminho do config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="coleta, avalia e notifica")
    r.add_argument("--dry-run", action="store_true", help="imprime no terminal, não envia nem grava")
    r.add_argument("--source", help="roda só esta fonte")
    r.add_argument("--limit", type=int, help="máximo de notificações nesta execução")
    r.add_argument("--with-claude", action="store_true",
                   help="no dry-run, chama o Claude de verdade (conta no limite diário)")
    sub.add_parser("status", help="uso do Claude, saúde das fontes, contadores")
    sub.add_parser("test-telegram", help="envia uma mensagem de teste")
    sub.add_parser("init-db", help="cria/migra o banco")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2
    dry = getattr(args, "dry_run", False)
    setup_logging(cfg.path("log_file"), quiet=dry)
    handlers = {"run": cmd_run, "status": cmd_status, "test-telegram": cmd_test_telegram, "init-db": cmd_init_db}
    try:
        return handlers[args.cmd](cfg, args)
    except ConfigError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2
