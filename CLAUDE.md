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
3. **Inteligência (Claude CLI)** — `ofertas/triage.py`. `claude -p --model haiku
   --output-format json --json-schema <SCHEMA> --system-prompt <nosso>
   --max-turns 2 --tools "" --strict-mcp-config --setting-sources ""
   --no-session-persistence`, prompt via stdin, cwd `data/claude-cwd/` (vazio,
   para não carregar este CLAUDE.md). `--max-turns 2` porque a saída
   estruturada consome um turno interno (verificado: `num_turns: 2`). A resposta
   vem em `structured_output`, validada por `ofertas/schema.py` (validador
   próprio, sem dependência). O Claude nunca navega; os textos das ofertas são
   tratados como dados (o prompt manda ignorar instruções neles).
   - Só são enviadas ao Claude as ofertas que **passaram na fase objetiva**;
     vereditos ficam em cache por (oferta, preço) na tabela `verdicts`.
   - O Claude recebe também `our_history` (observações, mediana, mínimo).
   - Campos de usuário vão truncados e sem caracteres de controle (`_clip`);
     a justificativa aparece na mensagem marcada como "🤖 Claude".
   - Lotes de `claude.batch_size` (20). Cada chamada conta em `usage` **antes**
     de ser feita; ao atingir `daily_call_limit` (100) para e avisa uma vez.
   - Custo medido: ~US$ 0,002 e ~13 s por lote de 5 ofertas.
4. **Critério** — `ofertas/criteria.py`, funções puras em duas fases:
   - `objective()`: loja permitida, dedupe e sinal de preço.
     - Com histórico (≥ `min_history_points` observações em `history_days`):
       preço ≤ mediana × (1 − `min_discount_vs_median`) **ou** novo menor preço
       (≥ `min_new_low_margin` abaixo do mínimo). O histórico exclui a
       observação da execução atual (`price_history(..., before=now)`).
     - Sem histórico: desconto declarado ≥ `min_declared_discount_alone` (40%,
       aprovado pelo usuário: não exige likes) **ou** ≥ `min_declared_discount`
       (30%) com sinal da comunidade. Marca "histórico insuficiente".
   - `with_verdict()`: nota ≥ `min_score`, não fake, não usado/recondicionado,
     categoria habilitada. `condition: desconhecido` é aceito. Sem veredito →
     `pending` (⏳ no dry-run). O Claude só pode **barrar**, nunca aprovar sozinho.
   O runner reavalia todas as ofertas listadas a cada execução (barato; o
   dedupe impede reenvio), para que uma oferta que esquenta depois tenha chance.
   O histórico vem de preços vistos nos agregadores (já são "promoções"), então
   a mediana é de preço promocional típico: 25% abaixo dela é bem raro.
   `ofertas report --days 7` mostra quantas passariam em cada limiar.
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
- `--dry-run` roda sobre `Storage.snapshot()` (cópia em memória): não envia nada
  e não altera o banco. Por padrão não chama o Claude (usa só vereditos em
  cache); com `--with-claude` chama, e o uso é contado no banco **real**.
- Testes: `pytest`, fixtures em `tests/fixtures/` (HTML/JSON salvos). Nenhum
  teste acessa a rede; HTTP é simulado com `httpx.MockTransport`.

## Comandos
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ofertas run --dry-run          # coleta real, sem enviar
.venv/bin/ofertas run --source promobit  # só uma fonte
.venv/bin/ofertas run --dry-run --with-claude  # idem, chamando o Claude
.venv/bin/ofertas test-telegram          # manda mensagem de teste
.venv/bin/ofertas status                 # uso do Claude hoje, saúde das fontes
.venv/bin/ofertas report --days 7        # calibração dos limiares
```

## Entrega em etapas
1. MVP: Promobit + Gatry, SQLite, dedupe, Telegram sem Claude. ✅ (critério
   provisório: desconto declarado + comunidade)
2. Claude CLI: classificar, normalizar nome, detectar fake, nota 0-100. ✅
3. Histórico de preços e critério estatístico (mediana 30-90d / menor preço). ✅
4. Mais fontes, comandos do bot, systemd, README de deploy.
