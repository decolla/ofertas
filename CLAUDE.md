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
   **Variações** (KaBuM! lista cada cor/modelo como produto): mesma loja, preço
   a ±3% e `same_family()` (Jaccard ≥ 0,6 dos tokens sem cores) → uma mensagem
   só, com "🎨 outras variações"; variação de algo enviado na janela de 72 h é
   pulada (`variant_skip`).
5. **Notificação** — `ofertas/notify.py`, HTTP direto na Bot API (sem
   python-telegram-bot). Chat privado único; comandos só do `TELEGRAM_CHAT_ID`.
   Com `image` → `sendPhoto` (legenda ≤ 1024); se o Telegram recusar a foto,
   cai para `sendMessage`. Gatry não tem foto. Promobit: caminho `/<id>.jpg`
   é servido pelo CDN `i.promobit.com.br` (`photo_url`).

## Regras de coleta (obrigatórias)
- Respeitar robots.txt (checado em `ofertas/http.py` antes de cada GET), ToS e
  rate limit por host. User-Agent honesto, definido em `config.yaml`.
- Nunca fazer login, nunca contornar CAPTCHA/Cloudflare/anti-bot. HTTP 403/429
  ou página de desafio → `BlockedError`: registrar e pular.
- Não sondar endpoints de API não documentados; usar só páginas públicas
  (HTML/JSON embutido renderizado para o navegador/crawler) ou RSS/API públicos.

## Estado das fontes (verificado em 2026-11-24)
Agregadores de comunidade cobrem todas as categorias e lojas (inclusive as que
bloqueiam robôs); por isso são a base. `Source` aceita `rotate_urls` +
`rotate_per_run` (rodízio por janela de 20 min, sem estado; `?page=2` é
ignorado por Promobit e Gatry, então só categorias/tags trazem ofertas a mais) e tolera falha de
uma página (só falha a fonte se todas falharem; `BlockedError` para na hora).
- **Promobit** ✅ `/promocoes/recentes/`, home, 29 páginas de categoria e 48
  de tag (`/promocoes/<tag>/s/`) em rodízio (10/execução): JSON `__NEXT_DATA__` com preço, preço antigo, cupom,
  likes, engajamento, tags, loja. Foto: `/<id>.jpg` → CDN `i.promobit.com.br`.
- **Pechinchou** ✅ `/recentes`, home e 23 `/categorias/<slug>` (4/execução): `__NEXT_DATA__` `pageProps.promos.results`
  (preço, preço antigo, loja, cupons, categoria, flag "menor preço"). Link
  `/oferta/<id>`. "Frete grátis assinantes" não conta como frete grátis.
- **Oferta Esperta** ✅ home, `/?sort=curtidas` e 24 `/categoria/<slug>`
  (5/execução): payload RSC (`self.__next_f.push`) com
  `initialOffers`; `"$undefined"` = vazio. Link = `href` `/promocao/<id>/<slug>`
  da própria página. Regras do cupom vão nas tags para o Claude.
- **Gatry** ✅ `/promocoes`: HTML SSR com título, preço, loja e link. Sem preço
  "de", votos nem foto → depende do nosso histórico de preços.
- **Pelando** ✅ `/recentes`, `/mais-quentes`, `/comentadas` e 8 comunidades
  `/c/<slug>` (3/execução): chave `initialDeals` no `props` da astro-island
  (nas `/c/` ela vem depois de `community`)
  (formato Astro `[tipo, valor]`, ver `astro_decode`). Sem preço "de";
  temperatura em graus → limites próprios em `criteria.fallback.per_source`.
  Posts `kind: coupon` são ignorados.
- **Comparajogos** ✅ home: cache Apollo em `__NEXT_DATA__`, lista
  `price_reduction` (maiores quedas do menor preço entre lojas de jogos de
  tabuleiro). O preço anterior é histórico real → `per_source.comparajogos.
  min_declared_discount_alone: 0.25`. Lista de desejos não conta como voto.
- **KaBuM!** ✅ páginas de campanha `/ofertas/<slug>` (descobertas na home a
  cada execução, até `max_campaigns`): `pageProps.dataOfferServer.data`. Preço
  usado = `priceWithDiscount` (PIX). Open box/pré-venda/indisponível são
  descartados; vendedor de marketplace vira loja "X (marketplace KaBuM!)". O
  preço "de" da KaBuM! costuma ser inflado: quem filtra é o Claude/histórico.
- **Fora** ❌ (bloqueiam robôs, não contornamos): Hardmob, Terabyte, Magalu,
  Pichau, Netshoes, Centauro, Riachuelo, Drogasil, Casas Bahia, Leroy Merlin,
  Mobly, Tok&Stok, Nike, Adidas, Galápagos, Ludopedia, Cuponomia, Pão de
  Açúcar. Amazon (Termos de Uso proíbem robôs); Mercado Livre (robots.txt
  bloqueia bots de IA). Zoom/Buscapé: preço sem sinal de desconto. As ofertas
  dessas lojas chegam pelos agregadores. O usuário quer cobertura direta de
  Amazon e ML **só por API oficial**: Amazon Creators API (PA-API 5 foi
  desativada em 2026) e API do ML (busca `/sites/MLB/search` com 403
  relatado; testar `/products/search`). Integrar só quando houver credenciais,
  validando contra a API real.
- Nunca usar User-Agent de navegador para "testar" uma página: sempre o UA
  honesto do `config.yaml`.

## Lojas e categorias
- `stores.mode: blacklist` (todas menos Shopee/AliExpress/Temu e lojas de jogos
  digitais). A whitelist antiga barrava Nike, Dafiti, Renner, farmácias etc.
- 19 categorias (`triage.CATEGORIES`, rótulos em `ofertas/categories.py`), todas
  ligadas por padrão; `/categorias` no bot mostra 2 botões por linha.

## Bot
- `ofertas bot` (processo contínuo, `ofertas-bot.service`): long polling,
  offset salvo em `settings.tg_offset`. `/pausar`, `/retomar`, `/categorias`
  (botões inline), `/minimo [n]` (nota mínima), `/status`, `/ajuda`.
- Estado em `settings` (`paused`, `categories`, `min_score`), lido pelo runner
  a cada ciclo; sobrepõe o `config.yaml`.
- `Telegram.call()` nunca loga a URL (contém o token); `httpx` fica em WARNING.

## Deploy
- `deploy/install.sh` renderiza `@ROOT@` nos units e instala em
  `~/.config/systemd/user`: `ofertas.timer` (`OnCalendar=*:0/20`) →
  `ofertas.service` (oneshot, PATH inclui `~/.local/bin` para o `claude`) e
  `ofertas-bot.service` (Restart=always).
- `claude.max_batches_per_run` (3) dilui backlogs grandes (ex.: 1ª execução
  com a KaBuM!) em várias execuções, maiores descontos primeiro.

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
.venv/bin/ofertas bot                    # comandos do Telegram (o systemd roda isso)
./deploy/install.sh                      # instala/liga timer + bot no systemd
```

## Entrega em etapas
1. MVP: Promobit + Gatry, SQLite, dedupe, Telegram sem Claude. ✅ (critério
   provisório: desconto declarado + comunidade)
2. Claude CLI: classificar, normalizar nome, detectar fake, nota 0-100. ✅
3. Histórico de preços e critério estatístico (mediana 30-90d / menor preço). ✅
4. Mais fontes, comandos do bot, systemd, README de deploy. ✅ (Pelando, KaBuM!)
5. Busca ampla (Black Friday): Pechinchou, Oferta Esperta, Comparajogos,
   categorias da Promobit em rodízio, lojas em blacklist, 19 categorias. ✅
   Depois: categorias em rodízio em Pechinchou, Oferta Esperta e Pelando, e
   tags da Promobit (~140 páginas, ~37 por execução). ✅
