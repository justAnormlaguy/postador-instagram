"""Publicador da Fase 2 — Graph API da Meta. STUB: não usar na Fase 1.

Fica aqui para marcar o ponto de encaixe e o que ele vai precisar. Quando
a Fase 2 começar, este é o único arquivo que ganha código de verdade
(mais um bucket para a imagem ter URL pública):

  1. Conta Instagram Profissional ligada a uma Página do Facebook.
  2. App no Meta for Developers + App Review de
     `instagram_content_publish` (e afins).
  3. Token de longa duração (~60 dias) + rotina de renovação.
  4. Imagem em URL pública: `posts.imagem_url` (bucket R2/S3). A captura
     já salva JPEG 1080x1080 — o formato que a API aceita.
  5. Publicação em duas chamadas:
       POST /{IG_USER_ID}/media          image_url, caption -> creation_id
       POST /{IG_USER_ID}/media_publish  creation_id        -> media id
  6. Limite de 25–50 posts por 24h em janela móvel — conferir com
       GET /{IG_USER_ID}/content_publishing_limit
  7. Rodar em PARALELO com a revisão humana antes de desligá-la, e fechar
     o post com o status `postada_via_api`.
"""

import os

GRAPH_BASE = "https://graph.facebook.com"
IG_USER_ID = os.environ.get("IG_USER_ID", "")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
LIMITE_POSTS_24H = 25  # o piso da faixa documentada; ajustar ao limite real


class PublicadorGraphAPI:
    nome = "graph_api"

    def entregar(self, con, post) -> bool:
        raise NotImplementedError(
            "Publicação pela Graph API é a Fase 2 — veja o docstring de "
            "postador/publicador/graph_api.py. Na Fase 1 use PUBLICADOR=humano."
        )
