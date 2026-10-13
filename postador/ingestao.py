"""Módulo 1 — ingestão. Grava notícias na fila com status `nova`.

A fonte principal é o banco do curador-noturno: o que ele entregou no
digest das 7h já é link + resumo, triado e deduplicado. O banco é aberto
**só para leitura** — o postador nunca escreve no curador.

O id do post é o mesmo `url_hash` do curador, então ingerir de novo não
duplica nada, e um link que já virou post não volta.
"""

import json
import sqlite3
from pathlib import Path

from .fila import cliente as fila


def ingerir_curador(con, caminho_curador: str | Path, *, horas: int = 48,
                    nota_minima: int = 0) -> dict:
    caminho = Path(caminho_curador).resolve()
    if not caminho.is_file():
        raise FileNotFoundError(f"banco do curador não encontrado: {caminho}")
    origem = sqlite3.connect(f"{caminho.as_uri()}?mode=ro", uri=True, timeout=30)
    origem.row_factory = sqlite3.Row
    try:
        # A janela evita que a primeira ingestão despeje meses de histórico
        # na fila: só entra o que foi coletado nas últimas `horas`.
        linhas = origem.execute(
            """SELECT url, titulo, resumo, nota FROM itens
               WHERE status='entregue'
                 AND resumo IS NOT NULL AND TRIM(resumo) <> ''
                 AND coletado_em >= ?
                 AND COALESCE(nota, 0) >= ?
               ORDER BY nota DESC, coletado_em ASC""",
            (fila.ha(horas), nota_minima),
        ).fetchall()
    finally:
        origem.close()

    novos = sum(
        fila.inserir(con, link=l["url"], resumo=l["resumo"],
                     titulo=l["titulo"], nota=l["nota"])
        for l in linhas
    )
    return {"lidos": len(linhas), "novos": novos}


def ingerir_jsonl(con, caminho: str | Path) -> dict:
    """Entrada genérica: uma notícia por linha, com `link` (ou `url`) e
    `resumo`; `titulo` e `nota` são opcionais. Serve para qualquer outro
    feed que não seja o curador."""
    lidos = novos = 0
    for n, linha in enumerate(Path(caminho).read_text(encoding="utf-8").splitlines(), 1):
        if not linha.strip():
            continue
        try:
            d = json.loads(linha)
            link, resumo = d.get("link") or d["url"], d["resumo"]
        except (json.JSONDecodeError, KeyError) as e:
            print(f"! linha {n} ignorada: {e}")
            continue
        lidos += 1
        novos += fila.inserir(con, link=link, resumo=resumo,
                              titulo=d.get("titulo"), nota=d.get("nota"))
    return {"lidos": lidos, "novos": novos}
