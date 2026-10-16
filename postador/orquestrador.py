"""Módulo 2 — orquestrador. Lê `nova`, dispara a Manus, grava o task_id e
passa para `gerando`. Também faz o polling de segurança (`recolher`): o
webhook é o caminho rápido, mas se ele não chegar — túnel caído, i3
reiniciado — o polling captura antes de o sandbox da Manus sumir (48h).
"""

from .captura import capturar
from .fila import Status
from .fila import cliente as fila
from .manus import cliente as manus
from .manus import prompt

# Tarefa que ainda está rodando depois disso é considerada travada: para,
# conta como falha e volta para a fila — muito antes do limite de 48h.
HORAS_TRAVADA = 12


def disparar(con, marca: dict, *, limite: int | None = None) -> dict:
    """Respeita `posts_por_dia` por dia de calendário (desde a meia-noite):
    rodar o ciclo duas vezes no mesmo dia não gasta crédito em dobro, e o
    que foi disparado ontem não come a cota de hoje. Disparos manuais e
    novas tentativas também contam — todos gastam crédito.

    `limite` é o disparo manual (`disparar --limite N`): dispara até N
    mesmo com a cota do dia cheia."""
    if limite is not None:
        vagas = limite
    else:
        teto = marca.get("posts_por_dia", 3)
        vagas = max(0, teto - fila.disparados_desde(con, fila.inicio_do_dia()))
    if not vagas:
        return {"disparados": 0, "motivo": "teto diário atingido"}

    disparados = falhas = 0
    for post in fila.listar(con, Status.NOVA, limite=vagas):
        texto = prompt.montar(marca, fonte_resumo=post["fonte_resumo"],
                              fonte_link=post["fonte_link"])
        try:
            tarefa = manus.criar_tarefa(
                texto, schema=prompt.SCHEMA_SAIDA,
                titulo=f"[post] {post['titulo'] or post['fonte_link']}",
                locale=marca.get("locale") or None,
            )
        except manus.ErroManus as e:
            # Nada foi criado na Manus: o post continua `nova`, sem gastar
            # tentativa, e o próximo ciclo tenta de novo.
            print(f"! post {post['id'][:8]}: task.create falhou: {e}")
            falhas += 1
            if "HTTP 4" in str(e):
                break  # chave, plano ou crédito: os próximos vão falhar igual
            continue
        ok = fila.mudar_status(
            con, post["id"], Status.NOVA, Status.GERANDO,
            f"task {tarefa['task_id']}",
            manus_task_id=tarefa["task_id"],
            manus_task_url=tarefa["task_url"],
            disparado_em=fila.agora(),
        )
        if ok:
            disparados += 1
            print(f"  -> {post['id'][:8]} task {tarefa['task_id']}")
    return {"disparados": disparados, "falhas": falhas}


def recolher(con, marca: dict, *, idade_min_minutos: int = 15) -> dict:
    """Polling de segurança sobre tudo que está `gerando` há algum tempo."""
    capturados = rodando = falhas = 0
    corte = fila.ha(idade_min_minutos / 60)
    travada = fila.ha(HORAS_TRAVADA)
    for post in fila.listar(con, Status.GERANDO, limite=200):
        if not post["manus_task_id"] or (post["disparado_em"] or "") > corte:
            continue
        try:
            res = manus.resultado(post["manus_task_id"])
        except manus.ErroManus as e:
            print(f"! post {post['id'][:8]}: consulta falhou: {e}")
            falhas += 1
            continue
        if res.estado == "rodando":
            if (post["disparado_em"] or "") < travada:
                try:
                    manus.parar(post["manus_task_id"])
                except manus.ErroManus:
                    pass
                fila.registrar_falha(con, post["id"], f"tarefa rodando há mais de {HORAS_TRAVADA}h")
                falhas += 1
            else:
                rodando += 1
            continue
        if capturar(con, post, res, marca):
            capturados += 1
    return {"capturados": capturados, "rodando": rodando, "falhas": falhas}
