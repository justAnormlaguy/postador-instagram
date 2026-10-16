"""Camada de estado. Webhook, bot e cron rodam em paralelo e todos passam
por aqui: a troca de status é um compare-and-swap no SQLite, então dois
caminhos chegando no mesmo post (webhook e polling, por exemplo) nunca
processam a mesma coisa duas vezes."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .tipos import Status, transicao_valida

RAIZ = Path(__file__).resolve().parent.parent.parent
CAMINHO_BANCO = RAIZ / "dados" / "postador.db"

MAX_TENTATIVAS = 3

# Colunas que podem ser gravadas junto com uma transição ou fora dela.
CAMPOS_EDITAVEIS = {
    "legenda", "titulo_arte", "imagem_path", "imagem_url", "avisos",
    "manus_task_id", "manus_task_url", "tentativas", "erro",
    "disparado_em", "gerado_em", "enviado_revisao_em", "postado_em",
    "publicador",
}


class TransicaoInvalida(ValueError):
    pass


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ha(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(
        timespec="seconds"
    )


def inicio_do_dia() -> str:
    """Meia-noite de hoje no fuso da máquina, em UTC (o formato das colunas).

    O teto diário conta a partir daqui, e não em 24h corridas: com o ciclo
    rodando sempre às 7h15, os disparos de ontem às 07:15:3x ainda caíam
    dentro da janela de hoje e travavam o dia inteiro."""
    meia_noite = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return meia_noite.astimezone(timezone.utc).isoformat(timespec="seconds")


def conectar(caminho: Path | str | None = None) -> sqlite3.Connection:
    caminho = Path(caminho or CAMINHO_BANCO)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho, timeout=30)
    con.row_factory = sqlite3.Row
    # WAL: o bot lê enquanto o webhook escreve, sem travar.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def inicializar(con: sqlite3.Connection) -> None:
    con.executescript((RAIZ / "schema.sql").read_text(encoding="utf-8"))
    con.commit()


def hash_url(url: str) -> str:
    """Mesma função do curador-noturno: o id do post é o url_hash de lá."""
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()[:32]


# ------------------------------------------------------------------ escrita

def inserir(con, *, link: str, resumo: str, titulo: str | None = None,
            nota: int | None = None) -> bool:
    """INSERT OR IGNORE pelo hash do link: ingerir o mesmo feed 10x não
    duplica nada, e um post já postado nunca volta para a fila."""
    pid = hash_url(link)
    cur = con.execute(
        """INSERT OR IGNORE INTO posts
           (id, fonte_link, fonte_resumo, titulo, nota, status, criado_em)
           VALUES (?,?,?,?,?,?,?)""",
        (pid, link.strip(), resumo.strip(), titulo, nota, Status.NOVA.value,
         agora()),
    )
    if cur.rowcount:
        _evento(con, pid, None, Status.NOVA.value, "ingestão")
    con.commit()
    return cur.rowcount > 0


def mudar_status(con, post_id: str, de: Status, para: Status,
                 detalhe: str | None = None, **campos) -> bool:
    """Troca de status atômica. Retorna False se o post não estava mais em
    `de` — outro caminho chegou primeiro, e quem perdeu só desiste."""
    de, para = Status(de), Status(para)
    if not transicao_valida(de, para):
        raise TransicaoInvalida(f"{de.value} -> {para.value} não é permitida")
    desconhecidos = set(campos) - CAMPOS_EDITAVEIS
    if desconhecidos:
        raise ValueError(f"campos desconhecidos: {desconhecidos}")

    sets = ["status=?"] + [f"{c}=?" for c in campos]
    valores = [para.value, *campos.values(), post_id, de.value]
    cur = con.execute(
        f"UPDATE posts SET {', '.join(sets)} WHERE id=? AND status=?", valores
    )
    if cur.rowcount != 1:
        con.rollback()
        return False
    _evento(con, post_id, de.value, para.value, detalhe)
    con.commit()
    return True


def atualizar(con, post_id: str, **campos) -> None:
    """Grava campos sem mexer no status (legenda editada, msg enviada...)."""
    desconhecidos = set(campos) - CAMPOS_EDITAVEIS
    if desconhecidos:
        raise ValueError(f"campos desconhecidos: {desconhecidos}")
    if not campos:
        return
    sets = ", ".join(f"{c}=?" for c in campos)
    con.execute(f"UPDATE posts SET {sets} WHERE id=?", [*campos.values(), post_id])
    con.commit()


def registrar_falha(con, post_id: str, erro: str) -> Status | None:
    """Falha da Manus: volta para `nova` (o próximo ciclo tenta de novo) ou,
    depois de MAX_TENTATIVAS, para `falha`. Um post quebrado nunca trava a
    fila nem fica girando para sempre gastando crédito.

    O manus_task_id antigo é zerado: se a tarefa ainda mandar um webhook
    atrasado, ele não acha mais o post. A URL fica, para você investigar."""
    linha = pegar(con, post_id)
    n = (linha["tentativas"] if linha else 0) + 1
    destino = Status.FALHA if n >= MAX_TENTATIVAS else Status.NOVA
    ok = mudar_status(
        con, post_id, Status.GERANDO, destino, erro[:300],
        tentativas=n, erro=erro[:500], manus_task_id=None,
    )
    return destino if ok else None


def editar_legenda(con, post_id: str, legenda: str, avisos: str | None) -> bool:
    """Edição na revisão. Só vale enquanto o post espera revisão: legenda
    de post já postado é histórico, não rascunho."""
    cur = con.execute(
        "UPDATE posts SET legenda=?, avisos=? WHERE id=? AND status=?",
        (legenda, avisos, post_id, Status.AGUARDANDO_REVISAO.value),
    )
    if cur.rowcount != 1:
        con.rollback()
        return False
    _evento(con, post_id, Status.AGUARDANDO_REVISAO.value,
            Status.AGUARDANDO_REVISAO.value, f"legenda editada ({len(legenda)} chars)")
    con.commit()
    return True


def _evento(con, post_id, de, para, detalhe) -> None:
    con.execute(
        "INSERT INTO eventos (post_id, de, para, detalhe, criado_em) VALUES (?,?,?,?,?)",
        (post_id, de, para, detalhe, agora()),
    )


# ------------------------------------------------------------------ leitura

def pegar(con, post_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()


def resolver_id(con, prefixo: str) -> sqlite3.Row | None:
    """Aceita o id inteiro ou os 8 primeiros caracteres que o bot mostra."""
    linhas = con.execute(
        "SELECT * FROM posts WHERE id LIKE ? LIMIT 2", (prefixo.strip() + "%",)
    ).fetchall()
    return linhas[0] if len(linhas) == 1 else None


def eventos(con, post_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM eventos WHERE post_id=? ORDER BY id", (post_id,)
    ).fetchall()


def pegar_por_task(con, task_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM posts WHERE manus_task_id=?", (task_id,)
    ).fetchone()


def listar(con, status: Status, limite: int = 50) -> list[sqlite3.Row]:
    """Nota maior primeiro; entre notas iguais, o mais velho passa na frente
    (mesma regra do digest do curador, para a fila andar)."""
    return con.execute(
        """SELECT * FROM posts WHERE status=?
           ORDER BY COALESCE(nota, 0) DESC, criado_em ASC LIMIT ?""",
        (Status(status).value, limite),
    ).fetchall()


def disparados_desde(con, desde_iso: str) -> int:
    linha = con.execute(
        "SELECT COUNT(*) c FROM eventos WHERE para='gerando' AND criado_em >= ?",
        (desde_iso,),
    ).fetchone()
    return linha["c"]


def estatisticas(con) -> dict:
    return {
        l["status"]: l["c"]
        for l in con.execute("SELECT status, COUNT(*) c FROM posts GROUP BY status")
    }


# ------------------------------------------------------- mensagens / kv

def registrar_mensagem(con, chat_id, message_id: int, post_id: str, tipo: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO mensagens (chat_id, message_id, post_id, tipo) VALUES (?,?,?,?)",
        (str(chat_id), message_id, post_id, tipo),
    )
    con.commit()


def post_da_mensagem(con, chat_id, message_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT post_id, tipo FROM mensagens WHERE chat_id=? AND message_id=?",
        (str(chat_id), message_id),
    ).fetchone()


def kv_ler(con, chave: str, padrao: str | None = None) -> str | None:
    linha = con.execute("SELECT valor FROM kv WHERE chave=?", (chave,)).fetchone()
    return linha["valor"] if linha else padrao


def kv_gravar(con, chave: str, valor: str) -> None:
    con.execute("INSERT OR REPLACE INTO kv (chave, valor) VALUES (?,?)", (chave, valor))
    con.commit()
