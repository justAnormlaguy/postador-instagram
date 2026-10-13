"""Wrapper da API v2 da Manus (https://open.manus.ai/docs/v2).

Fala REST direto via `requests`, como o llm.py do curador. Três coisas
importam aqui:

- tarefas são assíncronas e rodam num sandbox que some: os anexos têm URL
  temporária e os arquivos são apagados em 48h — quem chama `resultado()`
  precisa baixar a imagem na hora;
- o webhook é assinado com RSA-SHA256 (chave pública da Manus), não com
  segredo compartilhado — `verificar_assinatura()` implementa o esquema;
- 429/5xx são re-tentados com backoff; o resto vira ErroManus.
"""

import base64
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import requests

API_BASE = os.environ.get("MANUS_API_BASE", "https://api.manus.ai").rstrip("/")
API_KEY = os.environ.get("MANUS_API_KEY", "")
AGENT_PROFILE = os.environ.get("MANUS_AGENT_PROFILE", "standard").strip()
TIMEOUT_S = int(os.environ.get("MANUS_TIMEOUT", "60"))
TENTATIVAS = int(os.environ.get("MANUS_TENTATIVAS", "4"))
JANELA_ASSINATURA_S = 300  # a doc manda recusar mais velho que 5 min
TTL_CHAVE_S = 3600         # a doc manda cachear a chave pública ~1h

EXTENSOES_IMAGEM = (".png", ".jpg", ".jpeg", ".webp")


class ErroManus(RuntimeError):
    pass


def configurado() -> bool:
    return bool(API_KEY)


# --------------------------------------------------------------- HTTP

def _chamar(metodo: str, rota: str, *, params=None, json=None) -> dict:
    if not API_KEY:
        raise ErroManus("MANUS_API_KEY não definida (veja o .env)")
    url = f"{API_BASE}/v2/{rota}"
    ultimo = ""
    for tentativa in range(TENTATIVAS):
        try:
            r = requests.request(
                metodo, url, params=params, json=json, timeout=TIMEOUT_S,
                headers={"x-manus-api-key": API_KEY},
            )
        except requests.RequestException as e:
            ultimo = f"rede: {e}"
        else:
            if r.status_code == 429 or r.status_code >= 500:
                ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
                espera = _retry_after(r) or 2 ** (tentativa + 1)
                time.sleep(min(espera, 60))
                continue
            if r.status_code >= 400:
                raise ErroManus(f"{rota}: HTTP {r.status_code}: {r.text[:300]}")
            dados = r.json()
            if dados.get("ok") is False:
                raise ErroManus(f"{rota}: {str(dados)[:300]}")
            return dados
        time.sleep(2 ** (tentativa + 1))
    raise ErroManus(f"{rota}: desisti após {TENTATIVAS} tentativas ({ultimo})")


def _retry_after(r: requests.Response) -> float | None:
    try:
        return float(r.headers.get("Retry-After", ""))
    except ValueError:
        return None


# --------------------------------------------------------------- tarefas

def criar_tarefa(texto: str, *, schema: dict | None = None,
                 titulo: str | None = None, locale: str | None = None) -> dict:
    corpo = {
        "message": {"content": texto},
        "interactive_mode": False,  # sem perguntas: segue com o que tem
        "hide_in_task_list": False,
    }
    if AGENT_PROFILE:
        corpo["agent_profile"] = AGENT_PROFILE
    if schema:
        corpo["structured_output_schema"] = schema
    if titulo:
        corpo["title"] = titulo[:120]
    if locale:
        corpo["locale"] = locale
    dados = _chamar("POST", "task.create", json=corpo)
    return {"task_id": dados["task_id"], "task_url": dados.get("task_url")}


def detalhe(task_id: str) -> dict:
    return _chamar("GET", "task.detail", params={"task_id": task_id})["task"]


def mensagens(task_id: str) -> list[dict]:
    """Todos os eventos da tarefa, do mais antigo para o mais novo."""
    saida, cursor = [], None
    while True:
        params = {"task_id": task_id, "order": "asc", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        dados = _chamar("GET", "task.listMessages", params=params)
        saida.extend(dados.get("messages", []))
        if not dados.get("has_more") or not dados.get("next_cursor"):
            return saida
        cursor = dados["next_cursor"]


def parar(task_id: str) -> None:
    _chamar("POST", "task.stop", json={"task_id": task_id})


# --------------------------------------------------------------- resultado

@dataclass
class Anexo:
    nome: str
    url: str
    tipo: str = ""   # content_type, quando a API informa

    @property
    def e_imagem(self) -> bool:
        if self.tipo.startswith("image/") or self.tipo == "image":
            return True
        return self.nome.lower().endswith(EXTENSOES_IMAGEM)


@dataclass
class Resultado:
    estado: str                  # pronto | rodando | pergunta | erro
    legenda: str = ""
    titulo_arte: str = ""
    nome_imagem: str = ""
    anexos: list[Anexo] = field(default_factory=list)
    erro: str = ""
    estruturado_ok: bool = False

    def imagem(self) -> Anexo | None:
        """Prefere o arquivo que a própria Manus disse ser a imagem do post."""
        imagens = [a for a in self.anexos if a.e_imagem]
        if self.nome_imagem:
            alvo = PurePosixPath(self.nome_imagem).name.lower()
            for a in imagens:
                if PurePosixPath(a.nome).name.lower() == alvo:
                    return a
        return imagens[-1] if imagens else None


def _nome_do_anexo(a: dict) -> str:
    nome = a.get("filename") or a.get("file_name") or ""
    if not nome and a.get("url"):
        nome = PurePosixPath(unquote(urlparse(a["url"]).path)).name
    return nome


def resultado_do_webhook(task_detail: dict) -> Resultado:
    """Payload do evento `task_stopped`. Já vem com legenda e anexos —
    nenhuma chamada extra é necessária quando tudo está lá."""
    motivo = task_detail.get("stop_reason")
    anexos = [
        Anexo(_nome_do_anexo(a), a["url"], a.get("content_type", ""))
        for a in task_detail.get("attachments") or [] if a.get("url")
    ]
    if motivo == "ask":
        return Resultado("pergunta", erro=task_detail.get("message", "")[:500],
                         anexos=anexos)
    res = Resultado("pronto", anexos=anexos)
    _aplicar_estruturado(res, task_detail.get("structured_output"))
    if not res.legenda:
        res.legenda = task_detail.get("message", "")
    return res


def resultado(task_id: str, *, carencia_s: int = 600) -> Resultado:
    """Caminho do polling: consulta a tarefa e monta o mesmo Resultado que o
    webhook monta. `carencia_s`: depois que a tarefa para, a extração do
    structured output roda num passo separado; espera esse tempo antes de
    desistir dela e cair na última mensagem do agente."""
    task = detalhe(task_id)
    status = task.get("status")
    if status == "running" or task.get("has_running_background_jobs"):
        return Resultado("rodando")
    if status == "deleted":
        return Resultado("erro", erro="tarefa apagada na Manus")

    eventos = mensagens(task_id)
    anexos, ultima_fala, ultimo_erro, estruturado = [], "", "", None
    for ev in eventos:
        tipo = ev.get("type")
        if tipo == "assistant_message":
            msg = ev.get("assistant_message") or {}
            ultima_fala = msg.get("content") or ultima_fala
            for a in msg.get("attachments") or []:
                if a.get("url"):
                    anexos.append(Anexo(_nome_do_anexo(a), a["url"],
                                        a.get("content_type") or a.get("type", "")))
        elif tipo == "error_message":
            ultimo_erro = (ev.get("error_message") or {}).get("content", "")
        elif tipo == "structured_output_result":
            estruturado = ev.get("structured_output_result")

    if status == "error":
        return Resultado("erro", erro=ultimo_erro or "tarefa terminou em erro")
    if status == "waiting":
        return Resultado("pergunta", erro=ultima_fala[:500], anexos=anexos)

    # stopped
    if estruturado is None:
        parada_ha = time.time() - int(task.get("updated_at") or 0)
        if parada_ha < carencia_s:
            return Resultado("rodando")  # extração ainda não chegou
    res = Resultado("pronto", anexos=anexos)
    _aplicar_estruturado(res, estruturado)
    if not res.legenda:
        res.legenda = ultima_fala
    return res


def _aplicar_estruturado(res: Resultado, estruturado: dict | None) -> None:
    if not estruturado:
        return
    valor = estruturado.get("value") or {}
    res.estruturado_ok = bool(estruturado.get("success"))
    if res.estruturado_ok:
        res.legenda = (valor.get("legenda") or "").strip()
        res.titulo_arte = (valor.get("titulo_arte") or "").strip()
        res.nome_imagem = (valor.get("nome_arquivo_imagem") or "").strip()
    elif estruturado.get("error"):
        res.erro = f"structured output: {estruturado['error']}"[:500]


def baixar(url: str, destino, limite_bytes: int = 30 * 1024 * 1024) -> int:
    """Baixa um anexo. A URL é assinada e temporária: não guarde a URL,
    guarde o arquivo."""
    with requests.get(url, stream=True, timeout=TIMEOUT_S) as r:
        r.raise_for_status()
        total = 0
        with open(destino, "wb") as f:
            for bloco in r.iter_content(64 * 1024):
                total += len(bloco)
                if total > limite_bytes:
                    raise ErroManus(f"anexo maior que {limite_bytes} bytes")
                f.write(bloco)
    return total


# --------------------------------------------------------------- webhook

_trava_chave = threading.Lock()
_chave_cache: tuple[float, str] | None = None


def chave_publica(forcar: bool = False) -> str:
    global _chave_cache
    with _trava_chave:
        if (not forcar and _chave_cache
                and time.monotonic() - _chave_cache[0] < TTL_CHAVE_S):
            return _chave_cache[1]
        pem = _chamar("GET", "webhook.publicKey")["public_key"]
        _chave_cache = (time.monotonic(), pem)
        return pem


def verificar_assinatura(*, url: str, corpo: bytes, assinatura_b64: str,
                         timestamp: str, chave_pem: str | None = None) -> bool:
    """Esquema da doc: RSA-SHA256 (PKCS#1 v1.5) sobre
    `{timestamp}.{url}.{sha256_hex(corpo)}`, com janela de 5 minutos.
    `url` é a URL pública completa registrada na Manus, com query string."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        if abs(int(time.time()) - int(timestamp)) > JANELA_ASSINATURA_S:
            return False
        assinado = f"{timestamp}.{url}.{hashlib.sha256(corpo).hexdigest()}".encode()
        chave = serialization.load_pem_public_key((chave_pem or chave_publica()).encode())
        chave.verify(base64.b64decode(assinatura_b64), assinado,
                     padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def webhook_criar(url: str) -> dict:
    return _chamar("POST", "webhook.create", json={"url": url})["webhook"]


def webhook_listar() -> list[dict]:
    return _chamar("GET", "webhook.list").get("data", [])


def webhook_apagar(webhook_id: str) -> None:
    _chamar("POST", "webhook.delete", json={"webhook_id": webhook_id})


# --------------------------------------------------------------- conta

def eu() -> dict:
    return _chamar("GET", "user.me")


def creditos() -> int | None:
    try:
        return _chamar("GET", "usage.availableCredits")["data"]["total_credits"]
    except (ErroManus, KeyError):
        return None
