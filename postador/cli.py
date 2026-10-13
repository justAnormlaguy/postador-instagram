"""Interface única. Cada subcomando é uma etapa isolada e re-executável —
dá para rodar qualquer uma na mão para depurar sem refazer as outras.

Dois processos em produção:
  ciclo   — timer diário (7h15, depois do digest do curador): ingere,
            dispara a Manus, recolhe o que ficou pronto e entrega.
  servir  — serviço contínuo: webhook da Manus + bot do Telegram + polling
            de segurança a cada N minutos.
"""

import argparse
import os
import sys
import threading

from . import ingestao, orquestrador, publicador, revisao, webhook
from .fila import Status
from .fila import cliente as fila
from .manus import cliente as manus
from .manus import prompt

CURADOR_DB = os.environ.get("CURADOR_DB", "").strip()
MANUTENCAO_MIN = float(os.environ.get("MANUTENCAO_MIN", "15"))


def _con(args):
    con = fila.conectar(args.banco)
    fila.inicializar(con)
    return con


def _post(con, prefixo: str):
    post = fila.resolver_id(con, prefixo)
    if not post:
        print(f"✗ nenhum post (ou mais de um) começa com {prefixo!r}")
        sys.exit(1)
    return post


# ------------------------------------------------------------------ etapas

def cmd_init(args):
    _con(args)
    print(f"banco pronto em {args.banco or fila.CAMINHO_BANCO}")


def cmd_adicionar(args):
    con = _con(args)
    novo = fila.inserir(con, link=args.link, resumo=args.resumo, titulo=args.titulo)
    print("adicionado" if novo else "já estava na fila (mesmo link)")


def cmd_ingerir(args):
    con = _con(args)
    marca = prompt.carregar_marca()
    if args.jsonl:
        print(ingestao.ingerir_jsonl(con, args.jsonl))
    caminho = args.curador_db or CURADOR_DB
    if caminho:
        nota = marca.get("nota_minima", 0) if args.nota_minima is None else args.nota_minima
        print(ingestao.ingerir_curador(con, caminho, horas=args.horas, nota_minima=nota))
    elif not args.jsonl:
        print("✗ informe --curador-db, --jsonl ou CURADOR_DB no .env")
        sys.exit(1)


def cmd_disparar(args):
    con = _con(args)
    print(orquestrador.disparar(con, prompt.carregar_marca(), limite=args.limite))


def cmd_recolher(args):
    """Polling na mão: captura o que ficou pronto e já entrega no Telegram."""
    con = _con(args)
    print(orquestrador.recolher(con, prompt.carregar_marca(), idade_min_minutos=0))
    print(publicador.entregar_pendentes(con))


def cmd_entregar(args):
    con = _con(args)
    print(publicador.entregar_pendentes(con))


def cmd_ciclo(args):
    """O que o timer das 7h15 chama. Cada etapa é independente: se a Manus
    estiver fora, ainda assim entrega o que já estava pronto."""
    con = _con(args)
    marca = prompt.carregar_marca()

    if CURADOR_DB:
        print("== ingestão ==")
        try:
            print(ingestao.ingerir_curador(con, CURADOR_DB, horas=48,
                                           nota_minima=marca.get("nota_minima", 0)))
        except Exception as e:  # ingestão não pode derrubar o resto do ciclo
            print(f"! ingestão falhou: {e}")

    print("== recolher (polling de segurança) ==")
    print(orquestrador.recolher(con, marca))

    print("== disparar ==")
    if not manus.configurado():
        print("! MANUS_API_KEY ausente: nada disparado")
    else:
        print(orquestrador.disparar(con, marca))

    print("== entregar para revisão ==")
    print(publicador.entregar_pendentes(con))

    print("== status ==")
    cmd_status(args)


def cmd_servir(args):
    """Webhook + bot + manutenção no mesmo processo. Cada thread tem a
    própria conexão SQLite (conexões não atravessam threads)."""
    con = _con(args)
    marca = prompt.carregar_marca()
    parar = threading.Event()

    def manutencao():
        # Roda já na partida (o que ficou pronto com o serviço parado chega
        # na hora) e depois a cada MANUTENCAO_MIN.
        con_m = fila.conectar(args.banco)
        while True:
            try:
                r = orquestrador.recolher(con_m, prompt.carregar_marca())
                e = publicador.entregar_pendentes(con_m)
                if r["capturados"] or e["entregues"] or r["falhas"]:
                    print(f"manutenção: {r} {e}")
            except Exception as ex:
                print(f"! manutenção: {ex!r}")
            if parar.wait(MANUTENCAO_MIN * 60):
                return

    threading.Thread(target=manutencao, daemon=True, name="manutencao").start()
    if not args.sem_webhook:
        threading.Thread(target=webhook.servir, args=(parar,), daemon=True,
                         name="webhook").start()
    try:
        if revisao.configurado():
            revisao.loop(con, marca, parar)
        else:
            print("! Telegram não configurado: só webhook e manutenção rodando")
            parar.wait()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()


def cmd_webhook(args):
    _con(args)
    webhook.servir()


def cmd_bot(args):
    con = _con(args)
    revisao.loop(con, prompt.carregar_marca())


# ------------------------------------------------------------------ Manus

def cmd_checar_api(args):
    """Rode ANTES de automatizar: chave errada só apareceria às 7h15."""
    falhou = False
    if not manus.configurado():
        print("✗ MANUS_API_KEY não definida (veja o .env)")
        falhou = True
    else:
        try:
            eu = manus.eu()
            print(f"✓ Manus: autenticado como {eu.get('email') or eu.get('user_id')}")
            cred = manus.creditos()
            print(f"  créditos disponíveis: {cred if cred is not None else '?'}")
            manus.chave_publica()
            print("✓ Manus: chave pública do webhook obtida")
            hooks = manus.webhook_listar()
            ativos = [h for h in hooks if h.get("status") == "active"]
            alvo = webhook.URL_PUBLICA
            if alvo and any(h.get("url") == alvo for h in ativos):
                print(f"✓ Manus: webhook ativo em {webhook._mascarar(alvo)}")
            else:
                print("  webhook não registrado — o polling cobre, mas rode "
                      "`registrar-webhook` quando o túnel estiver de pé")
        except manus.ErroManus as e:
            print(f"✗ Manus: {e}")
            falhou = True

    if not revisao.configurado():
        print("✗ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID não definidos")
        falhou = True
    else:
        try:
            bot = revisao._api("getMe")
            print(f"✓ Telegram: bot @{bot.get('username')}")
        except revisao.ErroTelegram as e:
            print(f"✗ Telegram: {e}")
            falhou = True

    if CURADOR_DB:
        ok = os.path.isfile(CURADOR_DB)
        print(f"{'✓' if ok else '✗'} curador: {CURADOR_DB}")
        falhou |= not ok
    else:
        print("  CURADOR_DB vazio: ingestão só por --jsonl ou `adicionar`")
    sys.exit(1 if falhou else 0)


def cmd_registrar_webhook(args):
    url = args.url or webhook.URL_PUBLICA
    if not url:
        print("✗ defina MANUS_WEBHOOK_URL no .env ou passe --url")
        sys.exit(1)
    if not url.startswith("https://"):
        print("✗ a Manus exige HTTPS público")
        sys.exit(1)
    if webhook.URL_PUBLICA and url != webhook.URL_PUBLICA:
        print("! atenção: --url difere de MANUS_WEBHOOK_URL; a assinatura usa a do .env")
    print("A Manus vai mandar um teste agora: o `servir` precisa estar rodando.")
    h = manus.webhook_criar(url)
    print(f"✓ webhook {h.get('id')} {h.get('status')}")


def cmd_webhooks(args):
    for h in manus.webhook_listar():
        print(f"{h.get('id')}  {h.get('status'):8s}  {webhook._mascarar(h.get('url', ''))}")


def cmd_apagar_webhook(args):
    manus.webhook_apagar(args.id)
    print("apagado")


# ------------------------------------------------------------------ fila

def cmd_status(args):
    con = _con(args)
    print("posts por status:", fila.estatisticas(con))
    nao_enviados = [p for p in fila.listar(con, Status.AGUARDANDO_REVISAO, limite=100)
                    if not p["enviado_revisao_em"]]
    if nao_enviados:
        print(f"  {len(nao_enviados)} capturado(s) ainda não entregue(s) no Telegram "
              "— rode `entregar`")
    for p in fila.listar(con, Status.FALHA, limite=5):
        print(f"  falha {p['id'][:8]}: {p['erro']}")


def cmd_listar(args):
    con = _con(args)
    for p in fila.listar(con, Status(args.status), limite=args.limite):
        print(f"{p['id'][:8]}  nota={p['nota']}  {p['criado_em'][:16]}  "
              f"{(p['titulo'] or p['fonte_link'])[:80]}")


def cmd_mostrar(args):
    con = _con(args)
    p = _post(con, args.id)
    for chave in p.keys():
        if p[chave] is not None:
            print(f"{chave:20s} {p[chave]}")
    print("\neventos:")
    for e in fila.eventos(con, p["id"]):
        print(f"  {e['criado_em']}  {e['de'] or '-'} -> {e['para']}  {e['detalhe'] or ''}")


def cmd_ver_prompt(args):
    con = _con(args)
    p = _post(con, args.id)
    print(prompt.montar(prompt.carregar_marca(), fonte_resumo=p["fonte_resumo"],
                        fonte_link=p["fonte_link"]))


def cmd_reprocessar(args):
    """falha -> nova, com tentativas zeradas. Para depois de corrigir a causa."""
    con = _con(args)
    p = _post(con, args.id)
    if fila.mudar_status(con, p["id"], Status.FALHA, Status.NOVA, "reprocessar (CLI)",
                         tentativas=0, erro=None):
        print("de volta para `nova`")
    else:
        print(f"✗ só posts em `falha` voltam (este está {p['status']})")


def cmd_reabrir(args):
    """descartada -> aguardando_revisao, e reenvia no próximo ciclo."""
    con = _con(args)
    p = _post(con, args.id)
    if fila.mudar_status(con, p["id"], Status.DESCARTADA, Status.AGUARDANDO_REVISAO,
                         "reaberto (CLI)", enviado_revisao_em=None, publicador=None):
        print("reaberto; rode `entregar` para mandar de novo")
    else:
        print(f"✗ só posts `descartada` reabrem (este está {p['status']})")


def main(argv=None):
    p = argparse.ArgumentParser(prog="postador",
                                description="Notícias -> posts de Instagram (Manus + revisão)")
    p.add_argument("--banco", default=None, help="caminho do SQLite")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(nome, fn, ajuda):
        s = sub.add_parser(nome, help=ajuda)
        s.set_defaults(func=fn)
        return s

    add("init", cmd_init, "cria o banco")

    a = add("adicionar", cmd_adicionar, "põe uma notícia na fila na mão")
    a.add_argument("link")
    a.add_argument("resumo")
    a.add_argument("--titulo")

    i = add("ingerir", cmd_ingerir, "Módulo 1: curador/JSONL -> fila (nova)")
    i.add_argument("--curador-db", help="caminho do curador.db (padrão: CURADOR_DB)")
    i.add_argument("--jsonl", help="arquivo com {link, resumo, titulo?, nota?} por linha")
    i.add_argument("--horas", type=int, default=48, help="janela de coleta no curador")
    i.add_argument("--nota-minima", type=int, default=None)

    d = add("disparar", cmd_disparar, "Módulo 2: nova -> gerando (cria tarefas Manus)")
    d.add_argument("--limite", type=int, default=None, help="ignora posts_por_dia")
    add("recolher", cmd_recolher, "Módulo 3 por polling: captura o que ficou pronto")
    add("entregar", cmd_entregar, "Módulo 4: manda pendentes para revisão")
    add("ciclo", cmd_ciclo, "ingerir + recolher + disparar + entregar (timer diário)")

    s = add("servir", cmd_servir, "webhook + bot + manutenção (serviço contínuo)")
    s.add_argument("--sem-webhook", action="store_true", help="só bot e polling")
    add("webhook", cmd_webhook, "só o servidor do webhook")
    add("bot", cmd_bot, "só o bot do Telegram")

    add("checar-api", cmd_checar_api, "testa Manus, Telegram e o banco do curador")
    r = add("registrar-webhook", cmd_registrar_webhook, "registra a URL pública na Manus")
    r.add_argument("--url")
    add("webhooks", cmd_webhooks, "lista webhooks registrados na Manus")
    w = add("apagar-webhook", cmd_apagar_webhook, "remove um webhook da Manus")
    w.add_argument("id")

    add("status", cmd_status, "contagem por status e últimas falhas")
    l = add("listar", cmd_listar, "lista posts de um status")
    l.add_argument("status", choices=[s.value for s in Status])
    l.add_argument("--limite", type=int, default=30)
    for nome, fn, ajuda in [
        ("mostrar", cmd_mostrar, "detalhe e histórico de um post"),
        ("ver-prompt", cmd_ver_prompt, "imprime o prompt que vai para a Manus"),
        ("reprocessar", cmd_reprocessar, "falha -> nova"),
        ("reabrir", cmd_reabrir, "descartada -> aguardando_revisao"),
    ]:
        add(nome, fn, ajuda).add_argument("id", help="id ou os 8 primeiros caracteres")

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
