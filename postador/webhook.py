"""Módulo 3 — endpoint do webhook da Manus.

Servidor HTTP da stdlib, escutando em 127.0.0.1: quem o expõe para a
internet é um túnel (Cloudflare Tunnel, por exemplo) — o i3 não abre porta
no roteador. A URL pública registrada na Manus fica em MANUS_WEBHOOK_URL.

Segurança, em camadas:
  1. caminho com segredo (MANUS_WEBHOOK_SECRET) — corta varredura cega;
  2. assinatura RSA-SHA256 da Manus com janela de 5 min — a verificação de
     verdade. Requisição sem assinatura recebe 200 e é ignorada (é o teste
     de ativação da Manus); assinatura inválida recebe 401;
  3. nenhum dado do corpo é confiado sem achar o post pelo task_id.

A Manus exige resposta em até 10 s, então o 200 sai antes do processamento:
o download da imagem roda numa thread. Se a thread morrer no meio, o
polling do `recolher` pega o post depois — nada se perde.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import publicador
from .captura import capturar
from .fila import Status
from .fila import cliente as fila
from .manus import cliente as manus
from .manus import prompt

HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
PORTA = int(os.environ.get("WEBHOOK_PORTA", "8787"))
URL_PUBLICA = os.environ.get("MANUS_WEBHOOK_URL", "").strip()
SEGREDO = os.environ.get("MANUS_WEBHOOK_SECRET", "").strip()
LIMITE_CORPO = 1024 * 1024


def caminho_esperado() -> str:
    """O caminho que este servidor atende. Vem da URL pública, para que a
    URL registrada na Manus e o servidor nunca discordem."""
    if URL_PUBLICA:
        return urlparse(URL_PUBLICA).path or "/"
    return f"/manus/webhook/{SEGREDO}" if SEGREDO else "/manus/webhook"


def url_sugerida(base: str) -> str:
    base = base.rstrip("/")
    return f"{base}/manus/webhook/{SEGREDO}" if SEGREDO else f"{base}/manus/webhook"


class _Handler(BaseHTTPRequestHandler):
    server_version = "postador"

    def log_message(self, fmt, *args):  # sem IP/caminho com segredo no log
        pass

    def _responder(self, codigo: int, texto: str = "") -> None:
        corpo = texto.encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        if self.path == "/saude":
            self._responder(200, "ok")
        else:
            self._responder(404)

    def do_POST(self):
        if urlparse(self.path).path != caminho_esperado():
            self._responder(404)
            return
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho <= 0 or tamanho > LIMITE_CORPO:
            self._responder(400, "corpo inválido")
            return
        corpo = self.rfile.read(tamanho)
        assinatura = self.headers.get("X-Webhook-Signature")
        timestamp = self.headers.get("X-Webhook-Timestamp")

        if not assinatura or not timestamp:
            # Teste de ativação da Manus (ou curioso). Responde 200 e não
            # processa nada: sem assinatura, nada muda no estado.
            print("webhook: requisição sem assinatura — respondida e ignorada")
            self._responder(200, "ok")
            return
        if not URL_PUBLICA:
            print("! webhook: MANUS_WEBHOOK_URL vazia — impossível verificar assinatura")
            self._responder(500)
            return

        ok = _verificar(corpo, assinatura, timestamp)
        if not ok:
            print("! webhook: assinatura inválida — recusado")
            self._responder(401, "assinatura inválida")
            return
        try:
            evento = json.loads(corpo)
        except json.JSONDecodeError:
            self._responder(400, "json inválido")
            return

        self._responder(200, "ok")
        threading.Thread(target=processar_evento, args=(evento,), daemon=True).start()


def _verificar(corpo: bytes, assinatura: str, timestamp: str) -> bool:
    try:
        if manus.verificar_assinatura(url=URL_PUBLICA, corpo=corpo,
                                      assinatura_b64=assinatura, timestamp=timestamp):
            return True
        # A Manus pode ter rodado a chave: busca de novo antes de recusar.
        return manus.verificar_assinatura(
            url=URL_PUBLICA, corpo=corpo, assinatura_b64=assinatura,
            timestamp=timestamp, chave_pem=manus.chave_publica(forcar=True),
        )
    except manus.ErroManus as e:
        print(f"! webhook: não consegui buscar a chave pública: {e}")
        return False


def processar_evento(evento: dict, con=None) -> str:
    """Roteia por event_type. Tipos desconhecidos são ignorados (a doc pede
    isso: a Manus pode criar eventos novos)."""
    tipo = evento.get("event_type")
    detalhe = evento.get("task_detail") or {}
    task_id = detalhe.get("task_id")
    if tipo not in ("task_stopped", "task_deleted") or not task_id:
        return "ignorado"

    proprio = con is None
    con = con or fila.conectar()
    try:
        post = fila.pegar_por_task(con, task_id)
        if not post or post["status"] != Status.GERANDO.value:
            return "sem post gerando para esta tarefa"

        if tipo == "task_deleted":
            fila.registrar_falha(con, post["id"], "tarefa apagada na Manus antes da captura")
            return "falha registrada"

        marca = prompt.carregar_marca()
        res = manus.resultado_do_webhook(detalhe)
        if res.estado == "pronto" and (not res.estruturado_ok or not res.imagem()):
            # O evento às vezes chega antes da extração ou sem os anexos
            # completos: completa pela API, que tem a lista inteira.
            try:
                completo = manus.resultado(task_id, carencia_s=0)
                if completo.estado == "pronto":
                    res = completo
            except manus.ErroManus as e:
                print(f"! webhook: complemento via API falhou: {e}")

        if capturar(con, post, res, marca):
            print(f"webhook: post {post['id'][:8]} capturado")
            publicador.entregar_pendentes(con)
            return "capturado"
        return res.estado
    except Exception as e:  # thread solta: loga e deixa o polling resolver
        print(f"! webhook: processamento falhou: {e!r}")
        return "erro"
    finally:
        if proprio:
            con.close()


def servir(parar: threading.Event | None = None) -> None:
    servidor = ThreadingHTTPServer((HOST, PORTA), _Handler)
    servidor.daemon_threads = True
    print(f"webhook: ouvindo em http://{HOST}:{PORTA}{_mascarar(caminho_esperado())}")
    if parar:
        threading.Thread(target=lambda: (parar.wait(), servidor.shutdown()),
                         daemon=True).start()
    servidor.serve_forever()


def _mascarar(caminho: str) -> str:
    return caminho.replace(SEGREDO, "***") if SEGREDO else caminho
