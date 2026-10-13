-- Fila do postador. Nada é apagado: item não revisado só espera, e o
-- histórico é o que impede perder ou repostar uma notícia.

CREATE TABLE IF NOT EXISTS posts (
    id               TEXT    PRIMARY KEY,   -- hash do link (mesmo do curador)
    fonte_link       TEXT    NOT NULL,
    fonte_resumo     TEXT    NOT NULL,
    titulo           TEXT,                  -- título original, só para o revisor
    nota             INTEGER,               -- nota do curador, ordena o disparo

    -- máquina de estados (ver postador/fila/tipos.py):
    -- nova -> gerando -> aguardando_revisao -> postada
    --                                      \-> descartada
    --         gerando -> nova   (falha da Manus, ainda com tentativas)
    --         gerando -> falha  (esgotou tentativas; `reprocessar` volta p/ nova)
    status           TEXT    NOT NULL DEFAULT 'nova',

    legenda          TEXT,                  -- editável na revisão
    titulo_arte      TEXT,                  -- texto que a Manus pôs na imagem
    imagem_path      TEXT,                  -- arquivo local, baixado na hora
    imagem_url       TEXT,                  -- URL pública (Fase 2, bucket)
    avisos           TEXT,                  -- legenda longa, imagem fora de 1080x1080...

    manus_task_id    TEXT    UNIQUE,
    manus_task_url   TEXT,
    tentativas       INTEGER NOT NULL DEFAULT 0,
    erro             TEXT,

    criado_em        TEXT    NOT NULL,
    disparado_em     TEXT,                  -- quando foi para a Manus
    gerado_em        TEXT,                  -- quando a captura terminou
    enviado_revisao_em TEXT,                -- quando chegou no Telegram
    postado_em       TEXT,
    publicador       TEXT                   -- quem fechou: humano / graph_api
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, criado_em);

-- Toda mensagem do Telegram que fala de um post. Responder a qualquer uma
-- delas edita a legenda daquele post.
CREATE TABLE IF NOT EXISTS mensagens (
    chat_id     TEXT    NOT NULL,
    message_id  INTEGER NOT NULL,
    post_id     TEXT    NOT NULL,
    tipo        TEXT    NOT NULL,           -- imagem / legenda / pedido_edicao
    PRIMARY KEY (chat_id, message_id)
);

-- Trilha de auditoria: cada transição de status, com quem e por quê.
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY,
    post_id     TEXT    NOT NULL,
    de          TEXT,
    para        TEXT    NOT NULL,
    detalhe     TEXT,
    criado_em   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eventos_post ON eventos(post_id);

-- Estado solto: offset do getUpdates do Telegram, etc.
CREATE TABLE IF NOT EXISTS kv (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
