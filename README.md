# 🍌 Ofertas — monitor de "preço de banana" no Telegram

Coleta ofertas de agregadores e lojas brasileiras a cada 20 minutos, guarda o
histórico de preços em SQLite, pede ao Claude (Haiku, via `claude -p`) para
classificar e detectar desconto falso, e só manda para o seu Telegram o que é
realmente excepcional.

```
fontes (sem LLM) ──► SQLite (ofertas, histórico, enviadas)
                          │
              critério objetivo (loja, dedupe, preço vs. mediana)
                          │  só o que passou
                     Claude Haiku (categoria, fake, nota 0-100)
                          │  nota ≥ mínimo
                       Telegram
```

## Fontes

| Fonte | Status | Como |
|---|---|---|
| Promobit | ✅ | `__NEXT_DATA__` de `/promocoes/recentes/`, home, 29 categorias, 48 tags (`/s/`: tênis, roupas, panelas, jogos de tabuleiro, bebidas...) e 38 lojas (Netshoes, Centauro, Nike, Magalu, Pichau...), 14 por execução em rodízio |
| Pechinchou | ✅ | `__NEXT_DATA__` de `/recentes`, home e 23 categorias (4 por execução) |
| Oferta Esperta | ✅ | dados públicos (RSC) da home, mais curtidas, 24 categorias e 24 lojas (8 por execução) |
| Pelando | ✅ | dados públicos de `/recentes`, `/mais-quentes`, `/comentadas` e 8 comunidades `/c/` (3 por execução) |
| Gatry | ✅ | HTML público de `/promocoes` |
| Comparajogos | ✅ | maiores quedas de preço de jogos de tabuleiro (home) |
| KaBuM! | ✅ | páginas de campanha `/ofertas/<slug>`, descobertas na home |
| Netshoes, Centauro, Nike, Adidas, Magalu, Casas Bahia, Drogasil, Leroy Merlin, Terabyte, Pichau, Ludopedia… | ❌ | bloqueiam robôs (Cloudflare/403); não contornamos |
| Amazon, Mercado Livre | ❌ direto | Termos de Uso / robots.txt; ver "APIs oficiais" abaixo |

As ofertas das lojas bloqueadas **chegam pelos agregadores**, que cobrem todas
as categorias: roupa, tênis, mercado, beleza, casa, pet, brinquedos etc.
Lojas: todas, menos Shopee, AliExpress, Temu e lojas de jogos digitais
(`stores.blacklist` no `config.yaml`).

Regras de coleta: respeita robots.txt, intervalo mínimo por site, User-Agent
honesto, sem login, sem contornar CAPTCHA/anti-bot. Uma fonte quebrada não
derruba as outras; após 5 falhas seguidas você recebe um aviso no Telegram.

## Instalação

Requisitos: Linux com systemd, Python 3.12, [Claude Code](https://claude.com/claude-code)
instalado e logado (`claude` no PATH; usa sua assinatura).

```bash
git clone <este repositório> ofertas && cd ofertas
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                      # 179 testes, sem rede
```

### Bot do Telegram

1. No Telegram, fale com o **@BotFather** → `/newbot` → copie o token.
2. `cp .env.example .env` e cole o token em `TELEGRAM_BOT_TOKEN`.
3. Abra o seu bot, toque em **Iniciar** e mande "oi".
4. Descubra o seu chat_id:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9]*'
   ```
   e coloque em `TELEGRAM_CHAT_ID` no `.env`.
5. `.venv/bin/ofertas test-telegram` — deve chegar uma mensagem.

### Testar sem enviar nada

```bash
.venv/bin/ofertas run --dry-run                 # coleta real, mostra o que passaria e por quê
.venv/bin/ofertas run --dry-run --with-claude   # idem, chamando o Claude (conta no limite)
```

O dry-run trabalha numa cópia do banco em memória: não envia e não grava nada
(exceto o contador de chamadas ao Claude, para o teto valer sempre).

### Rodar de verdade (systemd, modo usuário)

```bash
./deploy/install.sh
```

Instala e liga:
- `ofertas.timer` → roda `ofertas run` a cada 20 min (com lock: nunca sobrepõe);
- `ofertas-bot.service` → atende os comandos do Telegram, reinicia sozinho.

Para continuar rodando com você deslogado: `sudo loginctl enable-linger $USER`.

Operação:
```bash
systemctl --user list-timers ofertas.timer       # próxima execução
journalctl --user -u ofertas.service -f          # logs das execuções
journalctl --user -u ofertas-bot.service -f      # logs do bot
systemctl --user stop ofertas.timer              # parar
.venv/bin/ofertas status                         # uso do Claude, fontes, contadores
.venv/bin/ofertas report --days 7                # calibrar limiares
```

Logs estruturados (JSON por linha) também ficam em `logs/ofertas.log`.

## Comandos do bot

| Comando | O que faz |
|---|---|
| `/status` | avisos ativos/pausados, nota mínima, categorias, uso do Claude, saúde das fontes |
| `/pausar` / `/retomar` | para/volta a enviar (a coleta continua e o histórico segue crescendo) |
| `/categorias` | botões para ligar/desligar categorias |
| `/minimo` | mostra a nota mínima; `/minimo 75` muda |
| `/quero air fryer` | lista de desejos: avisa com critério mais leve (menor preço, 10% abaixo da mediana ou 20% de desconto declarado; nota ≥ 65). `/quero` sozinho mostra a lista |
| `/naoquero 2` | tira o item 2 (ou o termo) da lista de desejos |
| `/silenciar iphone` | nunca avisa de ofertas com essas palavras (barra antes do Claude); `/silenciar` mostra a lista |
| `/dessilenciar 1` | volta a avisar |
| `/resumo` | enviadas hoje, uso do Claude e as que quase passaram |

Em cada oferta, **👍 Boa / 👎 Não curti** guardam seu voto; os últimos 20 vão
no prompt do Claude como exemplos do seu gosto. Às 21 h (`digest.hour`) chega
o **resumo do dia** sozinho. Quando a fonte passa a listar uma oferta enviada
como encerrada, a mensagem é editada para "❌ Oferta encerrada".

Só o seu `TELEGRAM_CHAT_ID` é obedecido.

## Quando uma oferta é enviada

Todas as condições, configuráveis em `config.yaml`:

1. **Preço** — com histórico (≥ 5 observações em 90 dias): ≥ 25% abaixo da
   mediana **ou** novo menor preço. Sem histórico: desconto declarado ≥ 40%; ou
   ≥ 30% com sinal da comunidade; ou post muito quente no Pelando (≥ 400°).
   Nesse caso a mensagem vem marcada "histórico insuficiente". KaBuM! sem
   histórico exige 60% (o preço "de" dela é inflado). Termos do `/quero` usam
   os limites mais leves de `criteria.wishlist`.
2. **Loja** fora da blacklist (`stores`), produto novo (não open box/usado),
   sem termo silenciado, oferta não encerrada na fonte.
3. **Claude**: nota ≥ 80 (ou o valor de `/minimo`), não é desconto falso,
   categoria ligada. Roupas, calçados e acessórios precisam de 8 pontos a
   menos, jogos de tabuleiro de 10, esporte e brinquedos de 5
   (`criteria.min_score_offsets`); marcas de `brands.favorites` (Nike,
   Adidas, Hering, Galápagos, LEGO, Tramontina...) de mais 3, nunca abaixo
   de 65.
4. **Não repetida**: o mesmo produto não volta em 72 h, a não ser que o preço
   caia mais 5%. Variações de cor/modelo da mesma loja e mesmo preço viram uma
   mensagem só.

As mensagens vêm com a foto do produto quando a fonte informa (todas menos a
Gatry).

### Custos do Claude

Teto de `claude.daily_call_limit` (100) chamadas/dia; ao atingir, para e avisa
uma vez no Telegram. Cada chamada avalia até 20 ofertas (~US$ 0,002 por lote
de 5, pela estimativa do CLI) e o veredito fica em cache por oferta e preço.
A fila vai em ordem de chance de dar nota boa: termos do `/quero`, depois
ofertas com histórico real, depois com sinal da comunidade, e só então pelo
desconto declarado.

## APIs oficiais (Amazon, Mercado Livre) — opcional, ainda não integradas

Coletar Amazon e Mercado Livre direto só por API oficial:

- **Amazon**: a Product Advertising API 5 foi desativada em 2026; a
  substituta é a **Creators API**, que exige conta no Amazon Associados e OAuth
  (Login with Amazon). Crie a conta e as credenciais e a fonte pode ser
  integrada em `ofertas/sources/`.
- **Mercado Livre**: crie um app em developers.mercadolivre.com.br. Atenção:
  há relatos de 403 no endpoint de busca `/sites/MLB/search` mesmo com token;
  o catálogo (`/products/search`) parece funcionar. Será validado com as suas
  credenciais antes de virar fonte.

## Estrutura

```
ofertas/
  sources/     uma fonte por módulo (parse puro + fetch)
  http.py      cliente educado: robots.txt, rate limit, retry, BlockedError
  storage.py   SQLite + migrações
  criteria.py  critério objetivo e aplicação do veredito (funções puras)
  triage.py    chamada ao claude -p, schema, teto diário
  notify.py    mensagens do Telegram
  bot.py       comandos do Telegram
  runner.py    um ciclo completo
config.yaml    tudo que é ajustável
deploy/        units do systemd + install.sh
tests/         pytest com fixtures HTML/JSON salvas
```
