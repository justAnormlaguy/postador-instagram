import os

from ..fila import Status
from ..fila import cliente as fila


def obter(nome: str | None = None):
    nome = (nome or os.environ.get("PUBLICADOR", "humano")).strip()
    if nome == "humano":
        from .humano import PublicadorHumano
        return PublicadorHumano()
    if nome == "graph_api":
        from .graph_api import PublicadorGraphAPI
        return PublicadorGraphAPI()
    raise ValueError(f"PUBLICADOR desconhecido: {nome!r} (use humano ou graph_api)")


def entregar_pendentes(con, publicador=None) -> dict:
    """Tudo que está `aguardando_revisao` e ainda não foi entregue. Roda no
    ciclo e logo depois de cada captura; falhou, tenta no próximo."""
    publicador = publicador or obter()
    pendentes = [
        p for p in fila.listar(con, Status.AGUARDANDO_REVISAO, limite=100)
        if not p["enviado_revisao_em"]
    ]
    entregues = sum(1 for p in pendentes if publicador.entregar(con, p))
    return {"pendentes": len(pendentes), "entregues": entregues}
