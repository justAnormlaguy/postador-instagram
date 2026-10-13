# Automação: Notícias de Segurança → Posts no Instagram

> **Spec de implementação para Claude Code.**
> Objetivo: transformar o feed diário de notícias de segurança (link + resumo) em posts de Instagram gerados por IA (Manus), com **revisão humana** na publicação.
> Fase atual: **MVP com revisão humana**. Fase futura: **publicação automática via Graph API da Meta** (desacoplada, plugável no final do fluxo).

---

## 1. Princípio de arquitetura

A geração do conteúdo é **desacoplada** da publicação. O "publicador" é o único módulo intercambiável:

- **Hoje:** o publicador é um humano (revisa, ajusta, posta pelo app).
- **Amanhã:** o publicador é a Graph API da Meta, encaixada no mesmo ponto do fluxo, sem refazer o resto.

Tudo é orquestrado em torno de uma **fila com status**. A fila é o coração do sistema e o que garante que a migração futura seja indolor.

---

## 2. Modelo de dados (a fila)

Fila persistente (planilha / Airtable / Notion / tabela SQL). Cada notícia é um registro:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | string | ID único da notícia (hash do link para evitar duplicata) |
| `fonte_link` | url | Link original da notícia |
| `fonte_resumo` | text | Resumo entregue pela automação existente |
| `status` | enum | `nova` → `gerando` → `aguardando_revisao` → `postada` / `descartada` |
| `legenda` | text | Legenda gerada pela Manus (editável na revisão) |
| `imagem_path` | string | Caminho/URL da imagem gerada |
| `manus_task_id` | string | ID da tarefa Manus (para rastreio) |
| `criado_em` | datetime | Timestamp de entrada na fila |
| `postado_em` | datetime | Timestamp da publicação (preenchido na revisão) |

**Transições de status válidas:**
```
nova            → gerando            (orquestrador dispara Manus)
gerando         → aguardando_revisao (webhook da Manus concluiu)
aguardando_revisao → postada         (humano aprovou e publicou)
aguardando_revisao → descartada      (humano reprovou)
```

> Regra: nada é apagado da fila. Itens não revisados apenas esperam. Isso previne perda e repostagem.

---

## 3. Fluxo detalhado (por módulo)

### Módulo 1 — Ingestão
- **Entrada:** feed diário existente (link + resumo).
- **Ação:** grava cada notícia na fila com `status = nova`. Usa hash do link como `id` para deduplicar.
- **Saída:** registros `nova` na fila.

### Módulo 2 — Orquestrador (gatilho)
- **Trigger:** cron diário.
- **Ação:** lê itens `nova`, dispara tarefa na Manus via `POST /tasks` (API v2, `https://api.manus.ai`), grava `manus_task_id`, muda status para `gerando`.
- **Prompt:** ver Anexo A.

### Módulo 3 — Geração (Manus AI)
- **Modelo assíncrono obrigatório.** Tarefas da Manus rodam em sandbox efêmero; arquivos são **apagados após 48h**.
- **Registrar webhook** na Manus. No evento de conclusão da tarefa:
  1. Handler recebe o `POST` no endpoint próprio.
  2. Busca o resultado (legenda + imagem) via API **imediatamente**.
  3. Salva imagem localmente/bucket, grava `legenda` e `imagem_path`.
  4. Muda status para `aguardando_revisao`.

### Módulo 4 — Entrega para revisão humana (publicador atual)
Monta o "pacote de post" e entrega onde a pessoa revisa. Escolher UMA:

- **Opção A — Bot Telegram/WhatsApp:** envia imagem + legenda; a pessoa posta pelo celular. Rápido, mobile-first.
- **Opção B — Notion/Airtable:** card com imagem anexada, legenda **editável**, botões de status. Melhor para histórico e múltiplos revisores.
- **Opção C — Pasta no Drive + planilha:** menor custo, sem bot.

A pessoa: (1) confere e corrige erro factual / tom, (2) aprova ou descarta, (3) posta manualmente e marca `postada`.

> A legenda deve ser **editável** no ponto de revisão — não apenas aprovar/reprovar. A IA acerta ~80%; os 20% precisam de ajuste sem voltar à Manus.

---

## 4. Stack sugerida

| Camada | Opção recomendada | Alternativas |
|--------|-------------------|--------------|
| Orquestrador | n8n (self-host) | Make, Zapier, script Python/Node |
| Fila | Airtable | Notion, Google Sheets, Postgres |
| Geração | Manus API v2 | — |
| Endpoint de webhook | Função serverless (Vercel/Cloudflare Workers) | servidor próprio |
| Entrega revisão | Bot Telegram | Notion, Drive |
| Storage de imagem | Bucket (R2/S3) *opcional nesta fase* | disco local |

---

## 5. Variáveis de ambiente

```env
# Manus
MANUS_API_KEY=
MANUS_API_BASE=https://api.manus.ai
MANUS_WEBHOOK_SECRET=

# Fila (exemplo Airtable)
AIRTABLE_API_KEY=
AIRTABLE_BASE_ID=
AIRTABLE_TABLE_NAME=

# Entrega de revisão (exemplo Telegram)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Storage opcional (já pensando na Fase 2)
BUCKET_ENDPOINT=
BUCKET_ACCESS_KEY=
BUCKET_SECRET_KEY=
BUCKET_NAME=

# Meta / Graph API — VAZIO na Fase 1, preencher na Fase 2
IG_USER_ID=
META_ACCESS_TOKEN=
META_APP_ID=
META_APP_SECRET=
```

---

## 6. Estrutura de projeto sugerida

```
.
├── CLAUDE.md                  # este plano (ou referência a ele)
├── .env.example
├── src/
│   ├── ingest.ts              # Módulo 1 — grava notícias na fila
│   ├── orchestrator.ts        # Módulo 2 — cron, dispara Manus
│   ├── webhook.ts             # Módulo 3 — recebe conclusão da Manus
│   ├── review-delivery.ts     # Módulo 4 — envia pacote ao humano
│   ├── queue/
│   │   ├── client.ts          # CRUD da fila + transições de status
│   │   └── types.ts           # enum de status, tipo do registro
│   ├── manus/
│   │   ├── client.ts          # wrapper da API v2
│   │   └── prompt.ts          # Anexo A
│   └── publisher/
│       ├── human.ts           # publicador atual (entrega p/ revisão)
│       └── graph-api.ts       # publicador futuro (stub na Fase 1)
└── README.md
```

---

## 7. Fases de implementação

### Fase 1 — MVP com revisão humana
```
[ ] Definir schema da fila e transições de status
[ ] Módulo 1: ingestão do feed → status `nova`
[ ] Wrapper da API Manus v2 (criar tarefa)
[ ] Prompt de geração (Anexo A) parametrizável
[ ] Módulo 2: cron lê `nova`, dispara Manus, status `gerando`
[ ] Endpoint de webhook + verificação de assinatura
[ ] Módulo 3: handler captura legenda+imagem na hora (<48h), status `aguardando_revisao`
[ ] Módulo 4: entrega para revisão (bot/Notion/Drive) com legenda editável
[ ] Fluxo humano: aprovar/editar/descartar → status final
[ ] Dedup por hash do link
[ ] Logs e tratamento de falha da tarefa Manus
```

### Fase 2 — Publicação automática (futuro)
```
[ ] Conta Instagram Profissional + Página Facebook
[ ] App no Meta for Developers + App Review das permissões
[ ] Rotina de renovação do token de longa duração (~60 dias)
[ ] Garantir imagem em URL pública (bucket)
[ ] publisher/graph-api.ts: POST /media → POST /media_publish
[ ] Rodar em PARALELO com revisão humana para comparar resultados
[ ] Novo status `postada_via_api`
[ ] Respeitar limite de 25–50 posts / 24h (janela móvel)
```

---

## 8. Impedimentos e restrições

### Resolvidos pela revisão humana (Fase 1)
- Configuração da Meta (conta Business, Página, App, App Review) — **não necessária agora**.
- Tokens de 60 dias + renovação automática — irrelevante por enquanto.
- Limite de 25–50 posts/24h por API — não se aplica a post manual.
- Hospedagem de imagem em URL pública — o humano recebe o arquivo direto.
- Política da Meta sobre conteúdo automatizado — a publicação não é automatizada.
- **Erro factual da IA** (versão de CVE, empresa trocada) — o revisor confere antes.
- **Copyright** — o revisor é a última linha de defesa.

### Permanecem na Fase 1
- **Efemeridade da Manus** — sandbox e arquivos somem; sem webhook + captura imediata (<48h) o resultado se perde.
- **Plano da Manus** — algumas funções da API exigem nível Team; verificar antes.
- **Custo por tarefa** — cada notícia = 1 geração (texto + imagem). Diário é baixo, mas acumula.
- **Direitos autorais** — instruir a Manus a **parafrasear** e gerar arte própria, nunca reusar a imagem do portal. O revisor valida, não substitui a instrução.
- **Gargalo humano** — se ninguém revisar num dia, a fila acumula (não perde, só espera).

---

## 9. Regra de ouro para migração sem retrabalho

O publicador é o **único** módulo que muda entre as fases. Portanto:

1. A Manus já deve gerar tudo no formato que a Graph API vai exigir depois: **imagem 1080×1080** e **legenda dentro do limite de caracteres**.
2. Manter a fila com status desde o início.
3. (Opcional) Já salvar a imagem num bucket com URL pública na Fase 1 — assim, ao ligar a API, o campo `image_url` já estará pronto e esse elo some.

---

## Anexo A — Prompt de geração (Manus)

```
Você é um social media de uma página de cibersegurança.

Com base no RESUMO e no LINK abaixo, produza:

1. LEGENDA para Instagram:
   - Tom: {TOM}  (ex.: informativo e direto, sem sensacionalismo)
   - Limite: {MAX_CARACTERES} caracteres
   - Parafraseie — NÃO copie o texto original da notícia
   - Inclua {N} hashtags relevantes do nicho de segurança
   - Não invente dados (versões de CVE, nomes de empresas, severidade);
     use apenas o que está no resumo

2. IMAGEM 1080x1080:
   - Template da marca: {DESCRICAO_TEMPLATE}
   - Arte própria — NÃO reutilize imagens de portais de notícia
   - Texto na imagem: título curto e legível em mobile

RESUMO: {fonte_resumo}
LINK: {fonte_link}

Retorne a legenda e o arquivo da imagem.
```
