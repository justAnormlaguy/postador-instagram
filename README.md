# Postador — notícias de segurança → posts de Instagram

Automação de homelab que transforma o feed diário de notícias de
cibersegurança em **posts de Instagram prontos para revisar**: a
[Manus](https://manus.im) escreve a legenda e cria a arte 1080×1350 (retrato 4:5), e o
pacote chega no seu **Telegram** com botões para aprovar, editar ou descartar.
Você posta pelo app; a máquina faz o resto.

```
07:00  curador-noturno  digest de notícias no Telegram
07:15  postador         lê as notícias entregues -> fila -> Manus (até 3/dia)
 ~min  Manus            gera legenda + imagem
       postador         captura na hora (webhook ou polling), normaliza a imagem
       Telegram         imagem + legenda + [✅ Postei] [✏️ Editar] [🗑 Descartar]
```

Foi feito para rodar ao lado do
[curador-noturno](https://github.com/justAnormlaguy/curador-noturno), que já
coleta, tria e resume as notícias. Mas a entrada é genérica: qualquer fonte
que produza **link + resumo** serve (arquivo JSONL ou um comando na mão).

> **Fase atual:** revisão humana — nenhum post vai para o Instagram sem você.
> **Fase futura:** publicação automática pela Graph API da Meta, plugada no
> mesmo ponto do fluxo sem refazer o resto.

---

## Sumário

- [Como funciona](#como-funciona)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Primeiro post](#primeiro-post)
- [Revisão no Telegram](#revisão-no-telegram)
- [Fontes de notícias](#fontes-de-notícias)
- [Webhook da Manus (opcional, recomendado)](#webhook-da-manus-opcional-recomendado)
- [Rodando como serviço](#rodando-como-serviço)
- [Referência da CLI](#referência-da-cli)
- [Configuração](#configuração)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Decisões de design](#decisões-de-design)
- [Problemas comuns](#problemas-comuns)
- [Atualizando](#atualizando)
- [Licença](#licença)
- [Fase 2 — Graph API](#fase-2--graph-api)

---

## Como funciona

Quatro módulos em volta de uma **fila com status** (SQLite):

| Módulo | O que faz | Arquivo |
|---|---|---|
| 1. Ingestão | grava cada notícia na fila como `nova`; o id é o hash do link, então nada duplica | `postador/ingestao.py` |
| 2. Orquestrador | pega as `nova` de maior nota, cria uma tarefa na Manus para cada uma (respeitando o teto diário) e passa para `gerando` | `postador/orquestrador.py` |
| 3. Captura | quando a Manus termina, baixa a imagem **na hora**, normaliza e passa para `aguardando_revisao` | `postador/webhook.py`, `postador/captura.py` |
| 4. Revisão | entrega o pacote no Telegram; seu clique fecha o post | `postador/revisao.py` |

```
nova -> gerando -> aguardando_revisao -> postada
          |                          \-> descartada   (reabrir -> aguardando_revisao)
          |-> nova    (falha da Manus, ainda com tentativa)
          \-> falha   (3 tentativas; reprocessar -> nova)
```

Nada é apagado da fila: item não revisado só espera, e o histórico impede
perder ou repostar uma notícia. Cada transição fica registrada com data e
motivo (`postador.cli mostrar <id>`).

---

## Requisitos

- **Linux com Python 3.10+** e systemd (feito num mini-PC de homelab; qualquer
  máquina sempre ligada serve).
- **Conta na Manus com acesso à API** — <https://manus.im> → Settings → API.
  Cada post consome créditos (1 tarefa = texto + imagem). Conta gratuita é
  forçada para o perfil `lite`.
- **Bot do Telegram** — `@BotFather` → `/newbot`, e o id do seu chat.
- **Uma fonte de notícias**: o banco do curador-noturno, um arquivo JSONL ou
  notícias adicionadas na mão.
- *Opcional:* um túnel HTTPS (Cloudflare Tunnel ou Tailscale Funnel) para o
  webhook. Sem ele, tudo funciona por polling.

---

## Instalação

```bash
git clone https://github.com/justAnormlaguy/postador-instagram.git ~/postador
cd ~/postador
```

**Dependências** — pacotes do sistema (recomendado: sem venv para quebrar em
upgrade do Python):

```bash
sudo apt install python3-yaml python3-requests python3-cryptography python3-pil
```

ou ambiente virtual:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# nesta rota, troque `python3` por `.venv/bin/python` em todos os comandos
```

O Pillow (`python3-pil`) é opcional, mas sem ele a imagem não é conferida nem
convertida para JPEG 1080×1350.

**Configuração:**

```bash
cp .env.example .env && chmod 600 .env
nano .env                  # MANUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CURADOR_DB
cp config/marca.yaml config/marca.local.yaml
nano config/marca.local.yaml   # tom, limite, hashtags, template visual, posts por dia
python3 -m postador.cli init
```

**Pegar o id do chat do Telegram:** mande qualquer mensagem para o seu bot e rode

```bash
curl -s "https://api.telegram.org/bot<SEU_TOKEN>/getUpdates" | grep -o '"id":[-0-9]*' | head -1
```

Pode usar o **mesmo bot do curador-noturno**: ele só envia mensagens e nunca
lê updates, então não há conflito.

**Verificar tudo:**

```bash
python3 eval/teste_fumaca.py           # sem chave e sem internet -> "TUDO OK ✓"
python3 -m postador.cli checar-api     # chave/créditos da Manus, bot, banco do curador
```

O teste de fumaça roda o fluxo inteiro com Manus e Telegram falsos: ingestão,
deduplicação, teto diário, webhook HTTP real com assinatura RSA, polling,
retry, normalização da imagem e os botões do bot. Se ele falhar, é problema
de instalação.

---

## Primeiro post

Terminal 1 — o serviço (deixe rodando):

```bash
python3 -m postador.cli servir --sem-webhook    # bot + polling
```

Terminal 2:

```bash
python3 -m postador.cli ingerir                 # fonte -> fila
python3 -m postador.cli listar nova
python3 -m postador.cli ver-prompt <id>         # o que vai para a Manus
python3 -m postador.cli disparar --limite 1
```

Quando a tarefa terminar (o link dela aparece em
`python3 -m postador.cli mostrar <id>`, campo `manus_task_url`):

```bash
python3 -m postador.cli recolher                # captura E entrega no Telegram
```

Sem rodar nada, o `servir` faz o mesmo sozinho a cada 15 minutos (e logo que
sobe). Os ids aceitam só os 8 primeiros caracteres, como o bot mostra.

Se a arte não ficar boa, o ajuste é no `descricao_template` de
`config/marca.local.yaml` — não no código.

---

## Revisão no Telegram

Cada post chega em duas mensagens:

1. **A imagem**, como documento (qualidade original, pronta para salvar e
   postar), com o contexto: título e link da notícia, nota do curador e
   **avisos** (legenda longa demais, imagem recortada etc.).
2. **A legenda**, em texto puro — segure e copie, cola exatamente o que vai
   para o Instagram — com os botões:

| Botão | Efeito |
|---|---|
| ✅ Postei | você já postou pelo app: o post vira `postada` com data e hora |
| ✏️ Editar | o bot pede a legenda nova; responda com o texto completo |
| 🗑 Descartar | pede confirmação e vira `descartada` (dá para reabrir pela CLI) |
| 🔁 Reenviar | manda o pacote de novo |

**Atalho de edição:** responda a *qualquer* mensagem do post com o texto novo.
A legenda é trocada e reenviada já corrigida, sem voltar à Manus — a IA acerta
a maior parte, e os ajustes não custam crédito.

Comandos no chat: `/fila` (contagem por status) e `/pendentes` (reenvia tudo
que espera revisão). O bot só obedece ao chat de `TELEGRAM_CHAT_ID`.

---

## Fontes de notícias

**curador-noturno** (padrão): aponte `CURADOR_DB` para o `dados/curador.db`
dele. O banco é aberto **só para leitura**. Entram os itens que foram para o
digest (`status = 'entregue'`), coletados nas últimas 48 h e com nota ≥
`nota_minima` do `marca.yaml`. Como o id do post é o mesmo hash de link do
curador, ingerir de novo nunca duplica.

**Arquivo JSONL** — uma notícia por linha; `titulo` e `nota` são opcionais:

```json
{"link": "https://exemplo.com/noticia", "resumo": "O que aconteceu, em 3 linhas.", "titulo": "Título", "nota": 9}
```

```bash
python3 -m postador.cli ingerir --jsonl noticias.jsonl
```

**Na mão:**

```bash
python3 -m postador.cli adicionar https://exemplo.com/noticia "Resumo da notícia." --titulo "Título"
```

---

## Webhook da Manus (opcional, recomendado)

Sem webhook, o post é capturado em até `MANUTENCAO_MIN` minutos (polling). Com
ele, segundos depois de a Manus terminar. A Manus só aceita **HTTPS público**;
se a máquina está atrás do roteador, um **túnel** leva a internet até o
`127.0.0.1:8787` dela, sem abrir porta nenhuma:

```
Manus ──HTTPS──> postador.seudominio.com ──túnel de saída──> 127.0.0.1:8787 (servir)
```

Segurança em camadas: o caminho da URL tem um segredo aleatório (corta
varredura cega), e cada evento é **assinado pela Manus com RSA-SHA256** e
verificado contra a chave pública dela, com janela de 5 minutos. Requisição
sem assinatura recebe 200 e é ignorada (é o teste de ativação da Manus);
assinatura inválida recebe 401.

Escolha **uma** rota: **A** se você tem um domínio na Cloudflare; **B** se não
tem (Tailscale Funnel, gratuito). Depois, as duas seguem pelo passo **C**.

### Pré-requisitos

```bash
timedatectl | grep synchronized        # precisa dizer "yes"
```

Relógio fora de hora = todo webhook recusado. Se der `no`:
`sudo timedatectl set-ntp true`.

Gere o segredo do caminho e guarde:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(24))"
# ex.: q7Vt3kX9-LmP2wZr8sYb1nHc4dJe6fUa
```

### A — Cloudflare Tunnel (com domínio)

Precisa de conta gratuita na Cloudflare com o domínio ativo nela.

**A1. Instalar o cloudflared**

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

**A2. Autorizar a máquina na sua conta**

```bash
cloudflared tunnel login
```

Abra o link impresso em qualquer computador, entre e escolha o domínio. O
comando termina sozinho, salvando `~/.cloudflared/cert.pem`.

**A3. Criar o túnel e o DNS**

```bash
cloudflared tunnel create postador          # anote o UUID impresso
cloudflared tunnel route dns postador postador.seudominio.com
```

**A4. Configurar como serviço do sistema**

```bash
UUID=cole-o-uuid-aqui
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/$UUID.json /etc/cloudflared/
sudo nano /etc/cloudflared/config.yml
```

```yaml
tunnel: 1a2b3c4d-0000-0000-0000-000000000000
credentials-file: /etc/cloudflared/1a2b3c4d-0000-0000-0000-000000000000.json

ingress:
  # Só o webhook e o /saude saem para a internet; o resto morre na Cloudflare.
  - hostname: postador.seudominio.com
    path: ^/(manus/webhook/.+|saude)$
    service: http://127.0.0.1:8787
  - service: http_status:404
```

```bash
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress rule https://postador.seudominio.com/saude
sudo cloudflared --config /etc/cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
systemctl status cloudflared --no-pager    # active (running)
```

**A5. Bot Fight Mode.** Em *Security → Bots* no painel da Cloudflare: se o
**Bot Fight Mode** estiver ligado, ele pode desafiar o POST da Manus (que não
é navegador) e o registro falha com 403. No plano gratuito ele não aceita
exceção por caminho: desligue.

### B — Tailscale Funnel (sem domínio)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                  # abra o link impresso e entre
sudo tailscale funnel --bg 8787    # na 1a vez, aprove HTTPS/Funnel no link impresso e repita
tailscale funnel status            # mostra https://<maquina>.<tailnet>.ts.net
```

A URL pública é o `*.ts.net` mostrado. O Funnel expõe a porta inteira, mas o
`servir` só responde no caminho com segredo e em `/saude`.

### C — Ligar o webhook no postador

**C1. `.env`** (troque domínio e segredo; **sem barra no final** — a URL
entra na assinatura e precisa ser idêntica à registrada):

```
MANUS_WEBHOOK_SECRET=q7Vt3kX9-LmP2wZr8sYb1nHc4dJe6fUa
MANUS_WEBHOOK_URL=https://postador.seudominio.com/manus/webhook/q7Vt3kX9-LmP2wZr8sYb1nHc4dJe6fUa
```

**C2. Reiniciar o `servir` com webhook** — o `.env` só é lido na partida:

```bash
python3 -m postador.cli servir
# webhook: ouvindo em http://127.0.0.1:8787/manus/webhook/***
```

**C3. Testar de fora para dentro:**

```bash
curl -s http://127.0.0.1:8787/saude; echo                      # ok  (local)
curl -s https://postador.seudominio.com/saude; echo             # ok  (pelo túnel)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' \
  https://postador.seudominio.com/manus/webhook/errado          # 404
curl -s -X POST -H 'Content-Type: application/json' -d '{"teste":1}' \
  https://postador.seudominio.com/manus/webhook/<SEGREDO>; echo  # ok
```

| Se der... | É... |
|---|---|
| `/saude` local falha | `servir` parado ou aberto com `--sem-webhook` |
| local ok, pelo túnel falha (502/530/1033) | túnel parado: `systemctl status cloudflared` / `tailscale funnel status` |
| pelo túnel dá 403 ou desafio | Bot Fight Mode (A5) |
| caminho com segredo dá 404 | caminho do `MANUS_WEBHOOK_URL` diferente, ou `servir` não reiniciado |

**C4. Registrar na Manus** (com o `servir` rodando — ela manda um teste na hora):

```bash
python3 -m postador.cli registrar-webhook   # ✓ webhook <id> active
python3 -m postador.cli checar-api          # ✓ Manus: webhook ativo em ...
```

Mudou a URL? Registre a nova e remova a antiga com
`python3 -m postador.cli apagar-webhook <id>` (`webhooks` lista os ids).

**C5. Ponta a ponta:** `python3 -m postador.cli disparar --limite 1`. Quando a
tarefa terminar, o log mostra `webhook: post 1a2b3c4d capturado` em segundos.
Se mostrar `assinatura inválida`, a URL do `.env` não bate com a registrada
ou o relógio está fora. Enquanto isso o polling segue capturando — nenhum post
se perde.

---

## Rodando como serviço

Dois units:

| Unit | Quando | O que faz |
|---|---|---|
| `postador-ciclo.timer` → `.service` | todo dia às 7h15 | ingere, recolhe, dispara a Manus, entrega para revisão |
| `postador-servico.service` | sempre | webhook + bot do Telegram + polling de segurança |

Os arquivos em `systemd/` vêm com `User=controlador` e
`/home/controlador/postador` — **ajuste usuário e caminhos** para os seus. Na
rota venv, troque o `ExecStart` pela linha comentada.

```bash
nano systemd/postador-ciclo.service systemd/postador-servico.service
sudo cp systemd/postador-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now postador-servico.service postador-ciclo.timer
systemctl list-timers 'postador-*'
journalctl -u postador-servico -f
```

O horário de 7h15 é para rodar depois do digest do curador-noturno (7h), que
é quando os itens viram `entregue`. Com outra fonte, mude o `OnCalendar`.

---

## Referência da CLI

Todos como `python3 -m postador.cli <comando>`, dentro da pasta do projeto.

| Comando | O que faz |
|---|---|
| `init` | cria o banco (`dados/postador.db`) |
| `ingerir [--curador-db P] [--jsonl P] [--horas 48] [--nota-minima N]` | Módulo 1: fonte → fila |
| `adicionar <link> "<resumo>" [--titulo T]` | põe uma notícia avulsa na fila |
| `disparar [--limite N]` | Módulo 2: cria tarefas na Manus (`--limite` ignora o teto diário) |
| `recolher` | captura o que ficou pronto e entrega no Telegram |
| `entregar` | reenvia ao Telegram o que foi capturado e não chegou |
| `ciclo` | ingerir + recolher + disparar + entregar (o que o timer chama) |
| `servir [--sem-webhook]` | serviço contínuo: webhook + bot + polling |
| `webhook` / `bot` | só uma das partes, para depurar |
| `checar-api` | confere Manus, Telegram e o banco do curador |
| `registrar-webhook [--url U]` / `webhooks` / `apagar-webhook <id>` | gerencia o webhook na Manus |
| `status` | contagem por status, últimas falhas, pendências de entrega |
| `listar <status>` | lista posts de um status |
| `mostrar <id>` | todos os campos e a trilha de eventos de um post |
| `ver-prompt <id>` | o prompt exato que vai para a Manus |
| `reprocessar <id>` | `falha` → `nova` (depois de corrigir a causa) |
| `reabrir <id>` | `descartada` → `aguardando_revisao` |

---

## Configuração

### `.env`

| Variável | Padrão | Para quê |
|---|---|---|
| `MANUS_API_KEY` | — | chave da API da Manus |
| `MANUS_AGENT_PROFILE` | `standard` | `standard`, `lite` (mais barato) ou `max` |
| `MANUS_WEBHOOK_SECRET` | — | segredo aleatório no caminho do webhook |
| `MANUS_WEBHOOK_URL` | — | URL pública completa registrada na Manus; vazio = só polling |
| `WEBHOOK_HOST` / `WEBHOOK_PORTA` | `127.0.0.1` / `8787` | onde o `servir` escuta |
| `CURADOR_DB` | — | banco do curador-noturno; vazio = só JSONL/`adicionar` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | bot e chat da revisão |
| `TELEGRAM_IMAGEM_COMO` | `documento` | `documento` (qualidade original) ou `foto` (comprimida) |
| `TELEGRAM_LONG_POLL` | `25` | segundos do long polling do bot |
| `MANUTENCAO_MIN` | `15` | intervalo do polling de segurança |
| `PUBLICADOR` | `humano` | `humano` (Fase 1) ou `graph_api` (Fase 2, ainda stub) |

As variáveis de bucket e da Meta já estão no `.env.example` para a Fase 2 e
ficam vazias por enquanto.

### `config/marca.yaml` / `config/marca.local.yaml`

A voz da página. É daqui que sai o prompt enviado à Manus
(`ver-prompt <id>` mostra o resultado). O `marca.yaml` é o padrão versionado;
a sua versão vai no `marca.local.yaml`, que fica fora do git e, se existir,
vale no lugar do padrão — `git pull` nunca conflita com ela.

| Campo | Para quê |
|---|---|
| `tom` | tom da legenda |
| `max_caracteres` | limite da legenda (teto do Instagram: 2200) |
| `n_hashtags` | quantidade de hashtags (teto: 30) |
| `descricao_template` | identidade visual descrita em texto: cores, fontes, assinatura |
| `locale` | idioma da saída na Manus; vazio = padrão da conta |
| `posts_por_dia` | teto de tarefas por 24 h (janela móvel) — controla o gasto de crédito |
| `nota_minima` | nota mínima do curador para virar post |

---

## Estrutura do projeto

```
.
├── postador/
│   ├── cli.py              # todos os comandos
│   ├── ingestao.py         # Módulo 1
│   ├── orquestrador.py     # Módulo 2 + polling de segurança
│   ├── webhook.py          # Módulo 3: servidor HTTP + verificação de assinatura
│   ├── captura.py          # Módulo 3: baixa e normaliza imagem, confere legenda
│   ├── revisao.py          # Módulo 4: bot do Telegram
│   ├── fila/
│   │   ├── tipos.py        # status e transições permitidas
│   │   └── cliente.py      # CRUD + troca de status atômica
│   ├── manus/
│   │   ├── cliente.py      # wrapper da API v2 da Manus
│   │   └── prompt.py       # prompt de geração + schema da saída
│   └── publicador/
│       ├── base.py         # contrato do publicador
│       ├── humano.py       # Fase 1: entrega para revisão
│       └── graph_api.py    # Fase 2: stub com checklist
├── config/marca.yaml       # parâmetros do prompt (padrão; o seu vai em marca.local.yaml)
├── eval/teste_fumaca.py    # teste do fluxo inteiro, offline
├── systemd/                # timer diário + serviço contínuo
└── schema.sql              # tabelas da fila
```

---

## Decisões de design

**A fila é o coração.** Toda troca de status é um compare-and-swap no SQLite
(`UPDATE ... WHERE id=? AND status=?`). Webhook e polling podem chegar no
mesmo post ao mesmo tempo: um ganha, o outro desiste — nada é processado duas
vezes.

**Webhook é o caminho rápido; polling é a garantia.** O sandbox da Manus some
em 48 h e as URLs dos anexos são temporárias. Se o túnel cair ou a máquina
reiniciar, o polling captura. Tarefa rodando há mais de 12 h é parada e conta
como falha — bem antes das 48 h.

**Saída travada por schema.** A legenda volta como campo de JSON (structured
output da Manus), sem texto em volta para limpar. A Manus também informa o
nome do arquivo da arte, então um rascunho anexado junto não vira o post por
engano.

**Formato da Fase 2 desde já.** A imagem é normalizada para JPEG 1080×1350, retrato 4:5
(o maior formato do feed, aceito pela Graph API), e a legenda é conferida contra 2200 caracteres e 30
hashtags. Se algo foi ajustado, o revisor recebe um aviso.

**Falhas não giram para sempre.** Erro da Manus devolve o post para `nova`;
na terceira vez ele vai para `falha` e para de gastar crédito até você mandar
reprocessar.

### Onde a implementação diverge do plano original

| O plano sugeria | Implementado | Motivo |
|---|---|---|
| TypeScript, n8n | Python, CLI + systemd | roda na mesma máquina e do mesmo jeito que o curador-noturno |
| Airtable | SQLite local | sem conta nem serviço externo para a fila |
| webhook serverless | servidor da stdlib + túnel | a Manus aceita qualquer HTTPS público; o túnel não abre porta no roteador |
| `MANUS_WEBHOOK_SECRET` como segredo HMAC | segredo vira parte do caminho | a Manus assina com **RSA** (chave pública dela), não com segredo compartilhado |
| `POST /tasks` | `POST /v2/task.create` | nome real do endpoint na API v2 |
| status do plano | + `falha` | sem ele, um post quebrado gira para sempre gastando crédito |

---

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `MANUS_API_KEY não definida` | comando rodado fora da pasta do projeto, ou `EnvironmentFile=` aponta para outro lugar |
| `task.create: HTTP 4xx` | chave inválida, plano sem acesso à API ou sem crédito — rode `checar-api` |
| post vai para `falha` com "sem imagem anexada" | a Manus não salvou a arte como arquivo; reforce no `descricao_template` e `reprocessar` |
| capturado, mas não chegou no Telegram | o envio falhou (motivo no log); `status` mostra a pendência e `entregar` reenvia |
| `! getUpdates: N quedas seguidas de conexão` | rede instável até o Telegram; quedas isoladas são silenciosas e normais |
| `webhook: assinatura inválida` | `MANUS_WEBHOOK_URL` diferente da registrada, ou relógio fora (`timedatectl`) |
| webhook nunca chega | túnel caído — o polling cobre; veja a tabela do passo C3 |
| ingestão lê 0 | o digest do curador ainda não rodou, ou `nota_minima` alta |
| ingestão `unable to open database` | `CURADOR_DB` errado, ou o usuário do serviço não lê a pasta do curador |
| botões não respondem | outro processo lendo `getUpdates` do mesmo bot, ou `TELEGRAM_CHAT_ID` diferente |
| aviso "recortada no centro" | a Manus gerou fora de 4:5 — confira se o título não foi cortado |

---

## Atualizando

Tudo que é seu fica fora do git (`.env`, `config/marca.local.yaml`, `dados/`),
então atualizar é:

```bash
cd ~/postador
git pull
python3 eval/teste_fumaca.py                    # TUDO OK ✓
sudo systemctl restart postador-servico
```

Se o pull trouxe mudança em `systemd/`, copie de novo para
`/etc/systemd/system/` e rode `sudo systemctl daemon-reload`.

---

## Licença

[MIT](LICENSE).

---

## Fase 2 — Graph API

Checklist em [`postador/publicador/graph_api.py`](postador/publicador/graph_api.py).
Já está pronto para ela: o status `postada_via_api`, a imagem em JPEG
1080×1350, a legenda conferida contra os limites e o campo `imagem_url` na
fila. Falta o bucket com URL pública, o app na Meta (com App Review) e o código
do publicador — o resto do fluxo não muda.
