"""Publicador da Fase 1: entrega o pacote para revisão humana no Telegram.
A pessoa confere, edita se precisar, posta pelo app e toca em ✅ Postei —
é isso que leva o post para `postada` (ver revisao.py)."""

from .. import revisao


class PublicadorHumano:
    nome = "humano"

    def entregar(self, con, post) -> bool:
        return revisao.enviar_pacote(con, post)
