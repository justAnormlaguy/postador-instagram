"""Módulo 3 — captura do resultado da Manus.

Webhook e polling chegam aqui com o mesmo `Resultado`. A regra é uma só:
a imagem é baixada **na hora** (o sandbox da Manus some em 48h e a URL do
anexo é temporária), e só depois o post vira `aguardando_revisao`.

A troca de status é compare-and-swap: se webhook e polling capturarem o
mesmo post ao mesmo tempo, um ganha e o outro descarta o próprio download.
"""

import os
import re
import threading
from pathlib import Path

from .fila import Status
from .fila import cliente as fila
from .manus import cliente as manus
from .manus.prompt import (ALTURA_IMAGEM, LARGURA_IMAGEM, LIMITE_HASHTAGS_IG,
                           LIMITE_LEGENDA_IG)

PASTA_IMAGENS = fila.RAIZ / "dados" / "imagens"

_trava = threading.Lock()


def capturar(con, post, res: manus.Resultado, marca: dict) -> bool:
    """Retorna True se o post passou para `aguardando_revisao` agora."""
    if res.estado == "rodando":
        return False
    if post["status"] != Status.GERANDO.value:
        return False  # outro caminho já resolveu

    if res.estado == "pergunta":
        # interactive_mode=False devia impedir isto. Se a Manus parou para
        # perguntar, a tarefa não anda sozinha: encerra e tenta de novo.
        _parar_silencioso(post["manus_task_id"])
        _falhar(con, post, f"Manus pediu input: {res.erro}")
        return False
    if res.estado == "erro":
        _falhar(con, post, f"tarefa Manus falhou: {res.erro}")
        return False

    legenda = (res.legenda or "").strip()
    if not legenda:
        _falhar(con, post, "Manus terminou sem legenda" + (f" ({res.erro})" if res.erro else ""))
        return False
    anexo = res.imagem()
    if not anexo:
        _falhar(con, post, "Manus terminou sem imagem anexada")
        return False

    with _trava:
        atual = fila.pegar(con, post["id"])
        if not atual or atual["status"] != Status.GERANDO.value:
            return False
        try:
            final, avisos_img = _salvar_imagem(post["id"], anexo)
        except Exception as e:  # rede, arquivo corrompido, anexo gigante...
            _falhar(con, atual, f"falha ao baixar a imagem: {e}")
            return False

        avisos = avisos_img + checar_legenda(legenda, marca)
        ok = fila.mudar_status(
            con, post["id"], Status.GERANDO, Status.AGUARDANDO_REVISAO,
            f"capturado de {post['manus_task_id']}",
            legenda=legenda,
            titulo_arte=res.titulo_arte or None,
            imagem_path=str(final),
            avisos="\n".join(avisos) or None,
            gerado_em=fila.agora(),
            erro=None,
        )
        if not ok:
            final.unlink(missing_ok=True)
        return ok


def _falhar(con, post, erro: str) -> None:
    destino = fila.registrar_falha(con, post["id"], erro)
    print(f"! post {post['id'][:8]}: {erro} -> {destino.value if destino else '?'}")


def _parar_silencioso(task_id: str | None) -> None:
    if not task_id:
        return
    try:
        manus.parar(task_id)
    except manus.ErroManus as e:
        print(f"! não consegui parar a tarefa {task_id}: {e}")


# ------------------------------------------------------------------ imagem

def _salvar_imagem(post_id: str, anexo: manus.Anexo) -> tuple[Path, list[str]]:
    """Baixa e normaliza para JPEG 1080x1350 (retrato 4:5) — formato que a
    Graph API aceita. Feito desde a Fase 1, a migração não precisa reprocessar nada.
    Sem Pillow, guarda o arquivo como veio e avisa o revisor."""
    PASTA_IMAGENS.mkdir(parents=True, exist_ok=True)
    ext = Path(anexo.nome).suffix.lower() or ".png"
    if ext not in manus.EXTENSOES_IMAGEM:
        ext = ".png"
    original = PASTA_IMAGENS / f"{post_id}.original{ext}"
    temporario = original.with_suffix(original.suffix + f".{os.getpid()}.tmp")
    manus.baixar(anexo.url, temporario)
    os.replace(temporario, original)

    try:
        from PIL import Image
    except ImportError:
        return original, ["Pillow ausente: imagem não conferida nem convertida"]

    avisos = []
    with Image.open(original) as img:
        img.load()
        largura, altura = img.size
        alvo = (LARGURA_IMAGEM, ALTURA_IMAGEM)
        if (largura, altura) != alvo:
            proporcao = LARGURA_IMAGEM / ALTURA_IMAGEM
            # Recorta no centro até 4:5 (sobra largura ou sobra altura) e
            # depois redimensiona. 1% de folga para arredondamento.
            if abs(largura / altura - proporcao) > 0.01 * proporcao:
                avisos.append(
                    f"imagem veio {largura}x{altura}, fora de 4:5: recortada no "
                    "centro — confira se o título não foi cortado"
                )
                if largura / altura > proporcao:
                    nova = round(altura * proporcao)
                    esq = (largura - nova) // 2
                    img = img.crop((esq, 0, esq + nova, altura))
                else:
                    nova = round(largura / proporcao)
                    topo = (altura - nova) // 2
                    img = img.crop((0, topo, largura, topo + nova))
            else:
                avisos.append(f"imagem veio {largura}x{altura}: redimensionada")
            img = img.resize(alvo, Image.LANCZOS)
        final = PASTA_IMAGENS / f"{post_id}.jpg"
        img.convert("RGB").save(final, "JPEG", quality=92)
    return final, avisos


# ------------------------------------------------------------------ legenda

def checar_legenda(legenda: str, marca: dict) -> list[str]:
    avisos = []
    n = len(legenda)
    if n > LIMITE_LEGENDA_IG:
        avisos.append(f"legenda com {n} caracteres: o Instagram corta em {LIMITE_LEGENDA_IG}")
    elif n > marca["max_caracteres"]:
        avisos.append(f"legenda com {n} caracteres (limite da marca: {marca['max_caracteres']})")
    hashtags = len(re.findall(r"(?<!\w)#\w+", legenda))
    if hashtags > LIMITE_HASHTAGS_IG:
        avisos.append(f"{hashtags} hashtags: o Instagram aceita no máximo {LIMITE_HASHTAGS_IG}")
    return avisos
