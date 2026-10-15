#!/usr/bin/env python3
"""Teste de fumaça: roda o fluxo inteiro com Manus e Telegram falsos.

Valida ingestão do curador, dedup por hash, teto diário, máquina de
estados, webhook de verdade (HTTP + assinatura RSA), polling, falha com
retry, normalização da imagem e o bot de revisão — SEM chave e sem
internet. Rode sempre que mexer no código:

    python eval/teste_fumaca.py
"""

import base64
import hashlib
import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from postador import captura, ingestao, orquestrador, publicador, revisao, webhook  # noqa: E402
from postador.fila import Status  # noqa: E402
from postador.fila import cliente as fila  # noqa: E402
from postador.fila.tipos import TRANSICOES  # noqa: E402
from postador.manus import cliente as manus  # noqa: E402
from postador.manus import prompt  # noqa: E402

FALHAS = []


def checar(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        FALHAS.append(msg)


# ------------------------------------------------------------------ fixtures

def curador_falso(caminho: Path) -> None:
    """Banco no formato do curador-noturno (só as colunas que lemos)."""
    con = sqlite3.connect(caminho)
    con.execute("""CREATE TABLE itens (id INTEGER PRIMARY KEY, url TEXT, titulo TEXT,
                   resumo TEXT, nota INTEGER, status TEXT, coletado_em TEXT)""")
    agora, velho = fila.agora(), fila.ha(24 * 10)
    con.executemany(
        "INSERT INTO itens (url, titulo, resumo, nota, status, coletado_em) VALUES (?,?,?,?,?,?)",
        [
            ("https://ex.test/a", "Falha crítica no nginx", "Resumo A.\nLinha 2.", 9, "entregue", agora),
            ("https://ex.test/b", "Ransomware em hospital", "Resumo B.", 9, "entregue", agora),
            ("https://ex.test/c", "Patch Tuesday", "Resumo C.", 8, "entregue", agora),
            ("https://ex.test/d", "Nota baixa", "Resumo D.", 6, "entregue", agora),
            ("https://ex.test/e", "Ainda não entregue", "Resumo E.", 9, "resumido", agora),
            ("https://ex.test/f", "Velho demais", "Resumo F.", 10, "entregue", velho),
        ],
    )
    con.commit()
    con.close()


def png(largura: int, altura: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (largura, altura), (11, 15, 20)).save(buf, "PNG")
    return buf.getvalue()


IMAGENS = {}  # url -> bytes servidos pelo "CDN" falso da Manus


def baixar_falso(url, destino, limite_bytes=0):
    if url not in IMAGENS:
        raise manus.ErroManus("404 no anexo")
    Path(destino).write_bytes(IMAGENS[url])
    return len(IMAGENS[url])


class TelegramFalso:
    def __init__(self):
        self.chamadas, self.proximo = [], 100

    def __call__(self, metodo, *, files=None, timeout_http=30, **params):
        self.chamadas.append((metodo, params))
        if metodo in ("sendMessage", "sendDocument", "sendPhoto"):
            self.proximo += 1
            return {"message_id": self.proximo}
        if metodo == "getMe":
            return {"username": "teste_bot"}
        return True

    def ultimas(self, metodo):
        return [p for m, p in self.chamadas if m == metodo]


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def assinar(chave, url: str, corpo: bytes, ts: int | None = None) -> dict:
    ts = str(ts if ts is not None else int(time.time()))
    conteudo = f"{ts}.{url}.{hashlib.sha256(corpo).hexdigest()}".encode()
    sig = chave.sign(conteudo, padding.PKCS1v15(), hashes.SHA256())
    return {"X-Webhook-Signature": base64.b64encode(sig).decode(),
            "X-Webhook-Timestamp": ts}


def post_http(url: str, corpo: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=corpo, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def esperar(cond, segundos=10.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ teste

def main():
    tmp = Path(tempfile.mkdtemp(prefix="postador-"))
    fila.CAMINHO_BANCO = tmp / "postador.db"
    captura.PASTA_IMAGENS = tmp / "imagens"
    con = fila.conectar()
    fila.inicializar(con)
    marca = prompt.carregar_marca()
    marca["posts_por_dia"] = 2

    print("\n[1] máquina de estados")
    checar(Status.POSTADA in TRANSICOES[Status.AGUARDANDO_REVISAO], "aguardando_revisao -> postada")
    checar(not TRANSICOES[Status.POSTADA], "postada é final")
    fila.inserir(con, link="https://ex.test/x", resumo="x")
    try:
        fila.mudar_status(con, fila.hash_url("https://ex.test/x"), Status.NOVA, Status.POSTADA)
        checar(False, "nova -> postada recusada")
    except fila.TransicaoInvalida:
        checar(True, "nova -> postada recusada")
    con.execute("DELETE FROM posts")
    con.execute("DELETE FROM eventos")
    con.commit()

    print("\n[2] ingestão do curador")
    curador = tmp / "curador.db"
    curador_falso(curador)
    r = ingestao.ingerir_curador(con, curador, horas=48, nota_minima=8)
    checar(r == {"lidos": 3, "novos": 3}, f"só entregue, recente e nota>=8 entra ({r})")
    r = ingestao.ingerir_curador(con, curador, horas=48, nota_minima=8)
    checar(r["novos"] == 0, "re-ingerir não duplica (hash do link)")
    id_a = fila.hash_url("https://ex.test/a")
    checar(fila.pegar(con, id_a)["status"] == "nova", "entra como `nova`")
    jsonl = tmp / "feed.jsonl"
    jsonl.write_text('{"url": "https://ex.test/a", "resumo": "dup"}\nlixo\n'
                     '{"link": "https://ex.test/g", "resumo": "G"}\n', encoding="utf-8")
    r = ingestao.ingerir_jsonl(con, jsonl)
    checar(r == {"lidos": 2, "novos": 1}, f"JSONL: ignora lixo e dedup ({r})")

    print("\n[3] prompt")
    texto = prompt.montar(marca, fonte_resumo="RES", fonte_link="https://l")
    checar("RES" in texto and "https://l" in texto and "1080x1350" in texto and "4:5" in texto,
           "resumo, link e formato 4:5 no prompt")
    checar(str(marca["max_caracteres"]) in texto and marca["tom"] in texto, "tom e limite vêm do marca.yaml")
    s = prompt.SCHEMA_SAIDA
    checar(s["additionalProperties"] is False and set(s["required"]) == set(s["properties"]),
           "schema no formato exigido pela Manus")

    local = tmp / "marca.local.yaml"
    local.write_text("tom: meu tom\nmax_caracteres: 9999\nn_hashtags: 5\n"
                     "descricao_template: x\nposts_por_dia: 1\n", encoding="utf-8")
    padrao_local, prompt.CAMINHO_MARCA_LOCAL = prompt.CAMINHO_MARCA_LOCAL, local
    m = prompt.carregar_marca()
    checar(m["tom"] == "meu tom" and m["max_caracteres"] == 2200,
           "marca.local.yaml vale no lugar do padrão (e respeita o teto de 2200)")
    prompt.CAMINHO_MARCA_LOCAL = padrao_local

    print("\n[4] orquestrador: disparo com teto diário")
    criadas = []

    def criar_falso(texto, **kw):
        criadas.append(kw)
        return {"task_id": f"task{len(criadas)}", "task_url": f"https://manus.im/app/task{len(criadas)}"}

    manus.criar_tarefa = criar_falso
    r = orquestrador.disparar(con, marca)
    checar(r["disparados"] == 2, f"dispara até posts_por_dia ({r})")
    checar(criadas[0]["schema"] is prompt.SCHEMA_SAIDA, "manda o structured output schema")
    r = orquestrador.disparar(con, marca)
    checar(r["disparados"] == 0, "segundo ciclo no mesmo dia não dispara (janela de 24h)")
    gerando = fila.listar(con, Status.GERANDO)
    checar(len(gerando) == 2 and all(p["nota"] == 9 for p in gerando), "nota maior vai primeiro")

    print("\n[5] assinatura do webhook")
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = chave.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    manus.chave_publica = lambda forcar=False: pem
    url = "https://postador.exemplo.test/manus/webhook/s3gr3d0"
    corpo = b'{"x": 1}'
    h = assinar(chave, url, corpo)
    v = lambda **kw: manus.verificar_assinatura(**{"url": url, "corpo": corpo,  # noqa: E731
        "assinatura_b64": h["X-Webhook-Signature"], "timestamp": h["X-Webhook-Timestamp"], **kw})
    checar(v(), "assinatura válida aceita")
    checar(not v(corpo=b'{"x": 2}'), "corpo adulterado recusado")
    checar(not v(url=url + "?a=1"), "URL diferente recusada")
    velho = assinar(chave, url, corpo, ts=int(time.time()) - 600)
    checar(not manus.verificar_assinatura(url=url, corpo=corpo, assinatura_b64=velho["X-Webhook-Signature"],
                                          timestamp=velho["X-Webhook-Timestamp"]), "timestamp > 5 min recusado")

    print("\n[6] webhook HTTP de ponta a ponta")
    manus.baixar = baixar_falso
    tg = TelegramFalso()
    revisao._api = tg
    revisao.TOKEN, revisao.CHAT_ID = "x", "42"
    porta = porta_livre()
    webhook.HOST, webhook.PORTA = "127.0.0.1", porta
    webhook.URL_PUBLICA = url
    parar = threading.Event()
    threading.Thread(target=webhook.servir, args=(parar,), daemon=True).start()
    local = f"http://127.0.0.1:{porta}/manus/webhook/s3gr3d0"
    checar(esperar(lambda: _vivo(porta)), "servidor no ar")

    alvo = fila.pegar_por_task(con, "task1")
    IMAGENS["https://cdn.test/arte.png"] = png(1200, 900)
    IMAGENS["https://cdn.test/rascunho.png"] = png(50, 50)
    evento = {
        "event_id": "e1", "event_type": "task_stopped",
        "task_detail": {
            "task_id": "task1", "task_title": "t", "task_url": "u",
            "message": "pronto", "stop_reason": "finish",
            "attachments": [
                {"file_name": "rascunho.png", "url": "https://cdn.test/rascunho.png", "size_bytes": 1},
                {"file_name": "arte.png", "url": "https://cdn.test/arte.png", "size_bytes": 1},
                {"file_name": "notas.md", "url": "https://cdn.test/notas.md", "size_bytes": 1},
            ],
            "structured_output": {"success": True, "error": None, "value": {
                "legenda": "Falha no nginx corrigida.\n\n#infosec #nginx",
                "titulo_arte": "nginx: corrija já", "nome_arquivo_imagem": "arte.png"}},
        },
    }
    corpo = json.dumps(evento).encode()
    checar(post_http(local.replace("s3gr3d0", "errado"), corpo, assinar(chave, url, corpo)) == 404,
           "caminho sem o segredo -> 404")
    checar(post_http(local, corpo, {}) == 200, "sem assinatura (teste de ativação) -> 200")
    time.sleep(0.3)
    checar(fila.pegar(con, alvo["id"])["status"] == "gerando", "...e nada muda no estado")
    ruim = assinar(chave, url, b"outro corpo")
    checar(post_http(local, corpo, ruim) == 401, "assinatura inválida -> 401")
    checar(post_http(local, corpo, assinar(chave, url, corpo)) == 200, "evento assinado -> 200")
    checar(esperar(lambda: fila.pegar(con, alvo["id"])["status"] == "aguardando_revisao"),
           "post capturado -> aguardando_revisao")
    p = fila.pegar(con, alvo["id"])
    checar(p["legenda"].startswith("Falha no nginx"), "legenda veio do structured output")
    checar(p["titulo_arte"] == "nginx: corrija já", "titulo_arte gravado")
    from PIL import Image
    with Image.open(p["imagem_path"]) as img:
        checar(img.size == (1080, 1350) and img.format == "JPEG",
               f"imagem certa (arte.png, não o rascunho) normalizada p/ JPEG 1080x1350 ({img.size})")
    checar("recortada" in (p["avisos"] or ""), "aviso de recorte para o revisor")
    checar(esperar(lambda: fila.pegar(con, alvo["id"])["enviado_revisao_em"] is not None),
           "pacote entregue no Telegram logo após a captura")
    checar(tg.ultimas("sendDocument") and tg.ultimas("sendMessage")[-1]["text"] == p["legenda"],
           "imagem como documento + legenda em texto puro")
    n_antes = len(tg.chamadas)
    checar(post_http(local, corpo, assinar(chave, url, corpo)) == 200, "evento repetido -> 200")
    time.sleep(0.3)
    checar(len(tg.chamadas) == n_antes, "evento repetido não reenvia nada (idempotente)")

    print("\n[6b] normalização para 4:5")
    for nome, tamanho, recorta in [("quadrada", (1080, 1080), True),
                                   ("4:5 grande", (2160, 2700), False),
                                   ("alta demais", (1080, 1920), True)]:
        url_img = f"https://cdn.test/{tamanho[0]}x{tamanho[1]}.png"
        IMAGENS[url_img] = png(*tamanho)
        final, avisos_img = captura._salvar_imagem(f"teste{tamanho[1]}", manus.Anexo("x.png", url_img))
        with Image.open(final) as img:
            checar(img.size == (1080, 1350) and ("recortada" in " ".join(avisos_img)) == recorta,
                   f"{nome} {tamanho} -> 1080x1350, recorte={recorta} ({avisos_img})")

    print("\n[7] polling e falhas")
    outro = fila.pegar_por_task(con, "task2")
    manus.resultado = lambda task_id, carencia_s=600: manus.Resultado("rodando")
    r = orquestrador.recolher(con, marca, idade_min_minutos=0)
    checar(r["rodando"] == 1 and fila.pegar(con, outro["id"])["status"] == "gerando", "rodando: espera")
    manus.resultado = lambda task_id, carencia_s=600: manus.Resultado("erro", erro="sandbox caiu")
    orquestrador.recolher(con, marca, idade_min_minutos=0)
    p = fila.pegar(con, outro["id"])
    checar(p["status"] == "nova" and p["tentativas"] == 1 and p["manus_task_id"] is None,
           "falha da Manus -> volta para `nova` com 1 tentativa")
    for i in range(fila.MAX_TENTATIVAS - 1):
        orquestrador.disparar(con, marca, limite=99)
        p = fila.pegar(con, outro["id"])
        orquestrador.recolher(con, marca, idade_min_minutos=0)
    checar(fila.pegar(con, outro["id"])["status"] == "falha", "esgotou tentativas -> `falha`")
    checar(fila.mudar_status(con, outro["id"], Status.FALHA, Status.NOVA, "teste", tentativas=0),
           "falha -> nova (reprocessar)")

    # polling capturando de verdade, com o payload de task.listMessages
    orquestrador.disparar(con, marca, limite=99)
    p = fila.pegar(con, outro["id"])
    IMAGENS["https://cdn.test/b.png"] = png(1080, 1350)
    resultado_real = manus.__dict__["_resultado_original"]
    manus._chamar = _api_manus_falsa(p["manus_task_id"])
    manus.resultado = resultado_real
    r = orquestrador.recolher(con, marca, idade_min_minutos=0)
    p = fila.pegar(con, outro["id"])
    checar(r["capturados"] == 3 and p["status"] == "aguardando_revisao",
           f"polling captura todos os `gerando` ({r})")
    checar(p["legenda"] == "Hospital atacado. #ransomware" and not p["avisos"],
           "polling lê structured_output_result e imagem 1080x1350 sem aviso")

    print("\n[8] revisão no Telegram")
    def clique(dados, chat="42"):
        revisao.processar_update(con, {"callback_query": {
            "id": "cq", "data": dados, "message": {"message_id": 999, "chat": {"id": chat}}}}, marca)

    def resposta(texto, reply_to, chat="42"):
        revisao.processar_update(con, {"message": {
            "message_id": 5000, "chat": {"id": chat}, "text": texto,
            "reply_to_message": {"message_id": reply_to}}}, marca)

    pid = alvo["id"]
    clique(f"ok:{pid}", chat="666")
    checar(fila.pegar(con, pid)["status"] == "aguardando_revisao", "clique de outro chat ignorado")
    msg_legenda = con.execute("SELECT message_id FROM mensagens WHERE post_id=? AND tipo='legenda'",
                              (pid,)).fetchone()["message_id"]
    resposta("Legenda corrigida pelo revisor. #infosec", msg_legenda)
    checar(fila.pegar(con, pid)["legenda"] == "Legenda corrigida pelo revisor. #infosec",
           "responder à legenda edita a legenda")
    checar(tg.ultimas("sendMessage")[-1]["text"] == "Legenda corrigida pelo revisor. #infosec",
           "legenda nova reenviada com botões")
    clique(f"ed:{pid}")
    pedido = tg.proximo
    resposta("Versão final. #infosec", pedido)
    checar(fila.pegar(con, pid)["legenda"] == "Versão final. #infosec", "fluxo ✏️ Editar + resposta")
    resposta("x" * 2300, pedido)
    checar("2200" in (fila.pegar(con, pid)["avisos"] or ""), "legenda longa demais gera aviso")
    resposta("Versão final. #infosec", pedido)
    clique(f"ok:{pid}")
    p = fila.pegar(con, pid)
    checar(p["status"] == "postada" and p["postado_em"] and p["publicador"] == "humano",
           "✅ Postei -> postada, com postado_em")
    resposta("tarde demais", msg_legenda)
    checar(fila.pegar(con, pid)["legenda"] == "Versão final. #infosec", "post postado não aceita edição")

    oid = outro["id"]
    publicador.entregar_pendentes(con)
    clique(f"rm?:{oid}")
    checar(fila.pegar(con, oid)["status"] == "aguardando_revisao", "🗑 pede confirmação antes")
    clique(f"rm!:{oid}")
    checar(fila.pegar(con, oid)["status"] == "descartada", "confirmação -> descartada")
    checar(fila.mudar_status(con, oid, Status.DESCARTADA, Status.AGUARDANDO_REVISAO, "t",
                             enviado_revisao_em=None), "descartada -> aguardando_revisao (reabrir)")
    checar(fila.pegar(con, oid) is not None, "nada é apagado da fila")

    eventos = fila.eventos(con, pid)
    checar([e["para"] for e in eventos][:3] == ["nova", "gerando", "aguardando_revisao"],
           "trilha de eventos completa")

    print("\n[9] bot resiste a queda de conexão do long polling")
    roteiro = [revisao.TimeoutTelegram("Read timed out"),
               revisao.TimeoutTelegram("Read timed out"),
               [{"update_id": 77, "message": {"message_id": 1, "chat": {"id": "42"},
                                             "text": "/fila"}}]]
    para_bot = threading.Event()
    enviados = []

    def api_instavel(metodo, *, files=None, timeout_http=30, **params):
        if metodo == "getUpdates":
            checar(params["timeout"] < timeout_http, "timeout HTTP maior que o long poll")
            if not roteiro:
                para_bot.set()
                return []
            passo = roteiro.pop(0)
            if isinstance(passo, Exception):
                raise passo
            return passo
        enviados.append((metodo, params))
        return {"message_id": 1}

    revisao._api = api_instavel
    espera_original = threading.Event.wait
    threading.Event.wait = lambda self, t=None: self.is_set()  # sem dormir no teste
    try:
        revisao.loop(con, marca, para_bot)
    finally:
        threading.Event.wait = espera_original
    checar(any(p.get("text", "").startswith("Fila:") for _, p in enviados),
           "depois de 2 quedas o bot volta e responde /fila")
    checar(fila.kv_ler(con, "telegram_offset") == "78", "offset salvo no banco")

    parar.set()
    print()
    if FALHAS:
        print(f"FALHOU: {len(FALHAS)} checagem(ns)")
        sys.exit(1)
    print("TUDO OK ✓")


def _vivo(porta) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/saude", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def _api_manus_falsa(task_id):
    """Simula task.detail + task.listMessages para o caminho do polling."""
    def chamar(metodo, rota, *, params=None, json=None):
        if rota == "task.detail":
            return {"ok": True, "task": {"id": task_id, "status": "stopped",
                                         "updated_at": int(time.time()) - 5}}
        if rota == "task.listMessages":
            return {"ok": True, "has_more": False, "messages": [
                {"type": "user_message", "user_message": {"content": "prompt"}},
                {"type": "assistant_message", "assistant_message": {
                    "content": "Pronto! ![arte](/home/ubuntu/b.png)",
                    "attachments": [{"type": "image", "filename": "b.png",
                                     "url": "https://cdn.test/b.png",
                                     "content_type": "image/png"}]}},
                {"type": "status_update", "status_update": {"agent_status": "stopped"}},
                {"type": "structured_output_result", "structured_output_result": {
                    "success": True, "error": None,
                    "value": {"legenda": "Hospital atacado. #ransomware",
                              "titulo_arte": "Ransomware", "nome_arquivo_imagem": "b.png"}}},
            ]}
        raise AssertionError(f"rota inesperada {rota}")
    return chamar


manus._resultado_original = manus.resultado

if __name__ == "__main__":
    main()
