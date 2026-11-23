# Ofertas — monitor de "preço de banana" → Telegram

Serviço Python 3.12 que coleta ofertas de sites brasileiros, guarda histórico em
SQLite e envia ao Telegram só as excepcionais. Roda nesta máquina Linux via
systemd user timer (a cada 20 min).

## Arquitetura (não misturar as camadas)
1. **Coleta (sem LLM)** — `ofertas/sources/*.py`. Uma fonte = um módulo com uma
   subclasse de `Source` que implementa `fetch() -> list[RawOffer]`. Parsers são
   funções puras `parse(html: str) -> list[RawOffer]`, testadas com fixtures.
   Uma fonte que falha nunca derruba as outras (o runner isola cada `fetch`).
2. **Armazenamento** — `ofertas/storage.py`, `sqlite3` puro, migrações por
   `PRAGMA user_version`. Tabelas: offers, price_history, sent, verdicts, usage,
   source_health, settings.
3. **Inteligência (Claude CLI)** — `ofertas/triage.py` (etapa 2). `claude -p`
   headless, `--model haiku`, `--output-format json`, `--json-schema`,
   `--max-turns 1`, sem ferramentas. O Claude só recebe dados já coletados; nunca
   navega. Lotes de ~20 ofertas. Teto diário de chamadas (padrão 100) na tabela
   `usage`; ao atingir, para e avisa no Telegram uma vez.
4. **Critério** — `ofertas/criteria.py`, função pura que devolve
   `Decision(ok, reasons)`. Tudo configurável em `config.yaml`.
5. **Notificação** — `ofertas/notify.py`, HTTP direto na Bot API (sem
   python-telegram-bot). Chat privado único; comandos só do `TELEGRAM_CHAT_ID`.

## Regras de coleta (obrigatórias)
- Respeitar robots.txt (checado em `ofertas/http.py` antes de cada GET), ToS e
  rate limit por host. User-Agent honesto, definido em `config.yaml`.
- Nunca fazer login, nunca contornar CAPTCHA/Cloudflare/anti-bot. HTTP 403/429
  ou página de desafio → `BlockedError`: registrar e pular.
- Não sondar endpoints de API não documentados; usar só páginas públicas
  (HTML/JSON embutido renderizado para o navegador/crawler) ou RSS/API públicos.

## Estado das fontes (verificado em 2026-11-23)
- **Promobit** ✅ `/promocoes/recentes/` e home: JSON `__NEXT_DATA__` com preço,
  preço antigo, cupom, likes, engajamento, tags (frete grátis), loja.
- **Gatry** ✅ `/promocoes`: HTML SSR com título, preço, loja e link. Sem preço
  "de" nem votos → depende do nosso histórico de preços.
- **Hardmob** ❌ desafio Cloudflare ("Just a moment...") até no RSS → fora.
- **Pelando** (etapa 4) — `/recentes` tem SSR; investigar antes de implementar.

## Convenções
- Código, identificadores e logs em inglês; **mensagens do bot em PT-BR**.
- Preços em `float` com 2 casas (BRL). `parse_brl("R$ 1.149,00") == 1149.0`.
- Dependências mínimas: `httpx`, `pyyaml`; dev: `pytest`. Pense duas vezes antes
  de adicionar outra.
- Segredos só em `.env` (fora do git); `.env.example` documenta as chaves.
- Logs estruturados JSON (uma linha por evento) em `logs/ofertas.log` + stderr.
- `--dry-run` roda tudo dentro de uma transação que sofre rollback e imprime no
  terminal: não envia nada e não altera o banco.
- Testes: `pytest`, fixtures em `tests/fixtures/` (HTML/JSON salvos). Nenhum
  teste acessa a rede; HTTP é simulado com `httpx.MockTransport`.

## Comandos
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ofertas run --dry-run          # coleta real, sem enviar
.venv/bin/ofertas run --source promobit  # só uma fonte
.venv/bin/ofertas test-telegram          # manda mensagem de teste
```

## Entrega em etapas
1. MVP: Promobit + Gatry, SQLite, dedupe, Telegram sem Claude. ✅ (critério
   provisório: desconto declarado + comunidade)
2. Claude CLI: classificar, normalizar nome, detectar fake, nota 0-100.
3. Histórico de preços e critério estatístico (mediana 30-90d / menor preço).
4. Mais fontes, comandos do bot, systemd, README de deploy.
