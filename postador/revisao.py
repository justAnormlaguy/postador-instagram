"""Módulo 4 — revisão humana no Telegram.

Cada post chega em duas mensagens:

  1. a imagem (como documento, para não perder qualidade) com o contexto:
     título original, link, nota do curador e avisos;
  2. a legenda, em texto puro — segurar e copiar cola exatamente o que vai
     para o Instagram — com os botões:  ✅ Postei · ✏️ Editar · 🗑 Descartar

Editar: toque em ✏️ e responda com a legenda nova, ou simplesmente
*responda* a qualquer mensagem do post com o texto novo. A legenda é
reenviada já corrigida, sem voltar à Manus.

O bot só obedece ao chat configurado em TELEGRAM_CHAT_ID.
"""

import json
import os
import threading
from datetime import datetime
from html import escape
from pathlib import Path

import requests

from .captura import checar_legenda
from .fila import Status
from .fila import cliente as fila

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
IMAGEM_COMO = os.environ.get("TELEGRAM_IMAGEM_COMO", "documento").strip()
LIMITE_LEGENDA_TG = 1024  # legenda de foto/documento no Telegram
LIMITE_TEXTO_TG = 4096


class ErroTelegram(RuntimeError):
    pass


class TimeoutTelegram(ErroTelegram):
    """A conexão caiu ou não respondeu a tempo. No long polling é rotina."""


# Long polling curto de propósito: roteador doméstico e CGNAT derrubam
# conexão TCP parada em ~30-60 s, e aí o getUpdates de 50 s morria em
# "Read timed out". 25 s fica abaixo disso; o custo é uma requisição vazia
# a cada 25 s.
LONG_POLL_S = int(os.environ.get("TELEGRAM_LONG_POLL", "25"))


def configurado() -> bool:
    return bool(TOKEN and CHAT_ID)


def _api(metodo: str, *, files=None, timeout_http: int = 30, **params) -> dict:
    if not TOKEN:
        raise ErroTelegram("TELEGRAM_BOT_TOKEN não definido (veja o .env)")
    url = f"https://api.telegram.org/bot{TOKEN}/{metodo}"
    try:
        if files:
            r = requests.post(url, data=params, files=files, timeout=timeout_http)
        else:
            r = requests.post(url, json=params, timeout=timeout_http)
    except (requests.Timeout, requests.ConnectionError) as e:
        raise TimeoutTelegram(f"{metodo}: {e}") from None
    except requests.RequestException as e:
        raise ErroTelegram(f"{metodo}: {e}") from None
    dados = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not dados.get("ok"):
        raise ErroTelegram(f"{metodo}: HTTP {r.status_code} {dados.get('description', r.text[:200])}")
    return dados["result"]


def _teclado(post_id: str, confirmar_descarte: bool = False) -> str:
    if confirmar_descarte:
        botoes = [[
            {"text": "🗑 Sim, descartar", "callback_data": f"rm!:{post_id}"},
            {"text": "↩️ Voltar", "callback_data": f"vt:{post_id}"},
        ]]
    else:
        botoes = [
            [{"text": "✅ Postei", "callback_data": f"ok:{post_id}"},
             {"text": "✏️ Editar", "callback_data": f"ed:{post_id}"}],
            [{"text": "🗑 Descartar", "callback_data": f"rm?:{post_id}"},
             {"text": "🔁 Reenviar", "callback_data": f"re:{post_id}"}],
        ]
    return json.dumps({"inline_keyboard": botoes})


def _local(iso: str | None) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")


# ------------------------------------------------------------------ envio

def enviar_pacote(con, post) -> bool:
    """Manda imagem + legenda. Só marca `enviado_revisao_em` se o Telegram
    confirmou: senão o próximo ciclo tenta de novo."""
    if not configurado():
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes — pacote não enviado:")
        print(f"  {post['id'][:8]} {post['imagem_path']}\n{post['legenda']}\n")
        return False

    contexto = [f"<b>📝 Post para revisão</b> · <code>{post['id'][:8]}</code>"]
    titulo = escape((post["titulo"] or post["fonte_link"])[:150])
    contexto.append(f'<a href="{escape(post["fonte_link"], quote=True)}">{titulo}</a>')
    if post["nota"] is not None:
        contexto.append(f"nota do curador: {post['nota']}")
    if post["avisos"]:
        contexto.append("⚠️ " + escape(post["avisos"]).replace("\n", "\n⚠️ "))
    contexto.append("Confira fatos (CVE, empresa, severidade) contra a fonte.")
    legenda_tg = "\n".join(contexto)[:LIMITE_LEGENDA_TG]

    try:
        caminho = Path(post["imagem_path"])
        with caminho.open("rb") as f:
            if IMAGEM_COMO == "foto":
                msg_img = _api("sendPhoto", chat_id=CHAT_ID, caption=legenda_tg,
                               parse_mode="HTML", files={"photo": (caminho.name, f)})
            else:
                msg_img = _api("sendDocument", chat_id=CHAT_ID, caption=legenda_tg,
                               parse_mode="HTML", files={"document": (caminho.name, f)},
                               timeout_http=120)
        fila.registrar_mensagem(con, CHAT_ID, msg_img["message_id"], post["id"], "imagem")
        _enviar_legenda(con, post["id"], post["legenda"])
    except (ErroTelegram, OSError) as e:
        print(f"! post {post['id'][:8]}: envio para revisão falhou: {e}")
        return False

    fila.atualizar(con, post["id"], enviado_revisao_em=fila.agora())
    return True


def _enviar_legenda(con, post_id: str, legenda: str) -> int:
    msg = _api("sendMessage", chat_id=CHAT_ID, text=legenda[:LIMITE_TEXTO_TG],
               disable_web_page_preview=True, reply_markup=_teclado(post_id))
    fila.registrar_mensagem(con, CHAT_ID, msg["message_id"], post_id, "legenda")
    return msg["message_id"]


def _fechar_mensagem(message_id: int, texto: str) -> None:
    """Troca o texto da mensagem da legenda e tira os botões."""
    try:
        _api("editMessageText", chat_id=CHAT_ID, message_id=message_id,
             text=texto[:LIMITE_TEXTO_TG], disable_web_page_preview=True)
    except ErroTelegram as e:
        print(f"! não consegui editar a mensagem {message_id}: {e}")


def avisar(texto: str) -> None:
    """Aviso solto (falhas, resumo do ciclo). Nunca derruba quem chamou."""
    if not configurado():
        print(texto)
        return
    try:
        _api("sendMessage", chat_id=CHAT_ID, text=texto[:LIMITE_TEXTO_TG],
             disable_web_page_preview=True)
    except ErroTelegram as e:
        print(f"! aviso não enviado: {e}")


# ------------------------------------------------------------------ updates

def processar_update(con, upd: dict, marca: dict) -> None:
    if "callback_query" in upd:
        _callback(con, upd["callback_query"], marca)
    elif "message" in upd:
        _mensagem(con, upd["message"], marca)


def _do_chat_certo(chat: dict | None) -> bool:
    return bool(chat) and str(chat.get("id")) == str(CHAT_ID)


def _callback(con, cq: dict, marca: dict) -> None:
    msg = cq.get("message") or {}
    resposta = ""
    try:
        if not _do_chat_certo(msg.get("chat")):
            resposta = "chat não autorizado"
            return
        acao, _, post_id = (cq.get("data") or "").partition(":")
        post = fila.pegar(con, post_id)
        if not post:
            resposta = "post não encontrado"
            return
        mid = msg["message_id"]

        if post["status"] != Status.AGUARDANDO_REVISAO.value and acao != "re":
            resposta = f"já está {post['status']}"
            _fechar_mensagem(mid, f"{post['legenda']}\n\n— {post['status']}")
            return

        if acao == "ok":
            if fila.mudar_status(con, post_id, Status.AGUARDANDO_REVISAO, Status.POSTADA,
                                 "aprovado no Telegram", postado_em=fila.agora(),
                                 publicador="humano"):
                resposta = "marcado como postado ✅"
                _fechar_mensagem(mid, f"{post['legenda']}\n\n✅ postada em {_local(fila.agora())}")
        elif acao == "ed":
            pedido = _api(
                "sendMessage", chat_id=CHAT_ID,
                text=f"✏️ Responda ESTA mensagem com a legenda nova completa "
                     f"(post {post_id[:8]}).",
                reply_markup='{"force_reply": true, "input_field_placeholder": "legenda nova"}',
            )
            fila.registrar_mensagem(con, CHAT_ID, pedido["message_id"], post_id, "pedido_edicao")
            resposta = "mande a legenda nova"
        elif acao == "rm?":
            _api("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid,
                 reply_markup=_teclado(post_id, confirmar_descarte=True))
            resposta = "confirme o descarte"
        elif acao == "vt":
            _api("editMessageReplyMarkup", chat_id=CHAT_ID, message_id=mid,
                 reply_markup=_teclado(post_id))
        elif acao == "rm!":
            if fila.mudar_status(con, post_id, Status.AGUARDANDO_REVISAO, Status.DESCARTADA,
                                 "descartado no Telegram", publicador="humano"):
                resposta = "descartado 🗑"
                _fechar_mensagem(mid, f"{post['legenda']}\n\n🗑 descartada")
        elif acao == "re":
            if post["status"] == Status.AGUARDANDO_REVISAO.value:
                enviar_pacote(con, post)
                resposta = "reenviado"
            else:
                resposta = f"já está {post['status']}"
    except ErroTelegram as e:
        print(f"! callback falhou: {e}")
        resposta = "erro — veja o log"
    finally:
        try:
            _api("answerCallbackQuery", callback_query_id=cq["id"], text=resposta[:190])
        except ErroTelegram:
            pass


def _mensagem(con, msg: dict, marca: dict) -> None:
    if not _do_chat_certo(msg.get("chat")):
        return
    texto = (msg.get("text") or "").strip()

    if texto.startswith("/fila"):
        stats = fila.estatisticas(con)
        avisar("Fila: " + (", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "vazia"))
        return
    if texto.startswith("/pendentes"):
        pendentes = fila.listar(con, Status.AGUARDANDO_REVISAO, limite=20)
        if not pendentes:
            avisar("Nada esperando revisão.")
        for p in pendentes:
            enviar_pacote(con, p)
        return

    alvo = msg.get("reply_to_message")
    if not alvo or not texto:
        return
    ref = fila.post_da_mensagem(con, CHAT_ID, alvo["message_id"])
    if not ref:
        return
    post_id = ref["post_id"]
    avisos = "\n".join(checar_legenda(texto, marca)) or None
    if not fila.editar_legenda(con, post_id, texto, avisos):
        post = fila.pegar(con, post_id)
        avisar(f"Post {post_id[:8]} já está {post['status'] if post else '?'}: legenda não alterada.")
        return
    try:
        if avisos:
            avisar(f"⚠️ {avisos}")
        _enviar_legenda(con, post_id, texto)
    except ErroTelegram as e:
        print(f"! legenda editada, mas reenvio falhou: {e}")


def loop(con, marca: dict, parar: threading.Event | None = None) -> None:
    """Long polling do getUpdates. O offset fica no banco: reiniciar o
    serviço não reprocessa clique antigo."""
    parar = parar or threading.Event()
    offset = int(fila.kv_ler(con, "telegram_offset", "0"))
    print(f"bot: escutando o chat {CHAT_ID}")
    quedas = 0
    while not parar.is_set():
        try:
            updates = _api("getUpdates", timeout_http=LONG_POLL_S + 15, offset=offset,
                           timeout=LONG_POLL_S,
                           allowed_updates=["message", "callback_query"])
            quedas = 0
        except TimeoutTelegram as e:
            # Rede piscou: tenta de novo na hora. Só reclama se persistir.
            quedas += 1
            if quedas % 5 == 0:
                print(f"! getUpdates: {quedas} quedas seguidas de conexão ({e})")
            parar.wait(min(2 * quedas, 30))
            continue
        except ErroTelegram as e:
            print(f"! getUpdates: {e}")
            parar.wait(10)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                processar_update(con, upd, marca)
            except Exception as e:  # um update ruim não derruba o bot
                print(f"! update {upd.get('update_id')} falhou: {e!r}")
            fila.kv_gravar(con, "telegram_offset", str(offset))
