"""Postador: notícias do curador-noturno -> posts de Instagram via Manus,
com revisão humana no Telegram."""

import os
from pathlib import Path


def _carregar_env() -> None:
    """Lê o .env da raiz sem sobrescrever o ambiente. Em produção o systemd
    já injeta tudo via EnvironmentFile; isto é para rodar na mão. Precisa
    acontecer aqui, antes dos submódulos lerem os.environ no import."""
    caminho = Path(__file__).resolve().parent.parent / ".env"
    if not caminho.is_file():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        valor = valor.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(chave.strip(), valor)


_carregar_env()
