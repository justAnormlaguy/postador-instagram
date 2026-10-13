"""Status da fila e as transições permitidas. É a única fonte da verdade:
o cliente da fila recusa qualquer mudança que não esteja aqui."""

from enum import Enum


class Status(str, Enum):
    NOVA = "nova"
    GERANDO = "gerando"
    AGUARDANDO_REVISAO = "aguardando_revisao"
    POSTADA = "postada"
    DESCARTADA = "descartada"
    FALHA = "falha"
    # Fase 2: publicação pela Graph API, em paralelo com a revisão humana.
    POSTADA_VIA_API = "postada_via_api"


TRANSICOES: dict[Status, set[Status]] = {
    Status.NOVA: {Status.GERANDO},
    Status.GERANDO: {
        Status.AGUARDANDO_REVISAO,  # captura concluída
        Status.NOVA,                # falhou, mas ainda tem tentativa
        Status.FALHA,               # esgotou tentativas
    },
    Status.AGUARDANDO_REVISAO: {
        Status.POSTADA,
        Status.DESCARTADA,
        Status.POSTADA_VIA_API,
    },
    # Saídas manuais pela CLI: nada é apagado, então tudo pode voltar.
    Status.FALHA: {Status.NOVA},
    Status.DESCARTADA: {Status.AGUARDANDO_REVISAO},
    Status.POSTADA: set(),
    Status.POSTADA_VIA_API: set(),
}


def transicao_valida(de: Status | str, para: Status | str) -> bool:
    return Status(para) in TRANSICOES.get(Status(de), set())
