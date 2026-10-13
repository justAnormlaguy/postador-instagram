"""Contrato do publicador — o ÚNICO módulo que muda entre as fases.

Fase 1: o publicador é um humano (recebe o pacote no Telegram e posta).
Fase 2: o publicador é a Graph API da Meta, plugada no mesmo ponto.
Tudo antes dele (ingestão, Manus, captura, fila) não muda.
"""

from typing import Protocol


class Publicador(Protocol):
    nome: str

    def entregar(self, con, post) -> bool:
        """Leva um post `aguardando_revisao` ao ponto de publicação.
        Retorna True se a entrega foi confirmada (e não precisa repetir)."""
        ...
