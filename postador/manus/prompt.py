"""Anexo A do plano: o prompt de geração, parametrizado por
config/marca.yaml. A saída sai travada por schema (structured output), então
a legenda chega como campo de JSON — sem texto em volta para limpar."""

from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent.parent
CAMINHO_MARCA = RAIZ / "config" / "marca.yaml"
# Cópia sua, fora do git: se existir, vale no lugar do padrão. Assim o
# `git pull` nunca conflita com a voz da sua página.
CAMINHO_MARCA_LOCAL = RAIZ / "config" / "marca.local.yaml"

# Limites do Instagram. Também são os da Graph API — gerar dentro deles
# desde a Fase 1 é o que deixa a migração sem retrabalho.
LIMITE_LEGENDA_IG = 2200
LIMITE_HASHTAGS_IG = 30
LADO_IMAGEM = 1080

TEMPLATE = """\
Você é um social media de uma página de cibersegurança.

Com base no RESUMO e no LINK abaixo, produza:

1. LEGENDA para Instagram:
   - Tom: {tom}
   - Limite: {max_caracteres} caracteres, contando as hashtags
   - Parafraseie — NÃO copie o texto original da notícia
   - Inclua {n_hashtags} hashtags relevantes do nicho de segurança, no final
   - Não invente dados (versões de CVE, nomes de empresas, severidade);
     use apenas o que está no resumo
   - Escreva em português do Brasil

2. IMAGEM {lado}x{lado} (quadrada, PNG ou JPG):
   - Template da marca: {descricao_template}
   - Arte própria — NÃO reutilize imagens de portais de notícia nem logos
     de terceiros
   - Texto na imagem: título curto (até 8 palavras) e legível em mobile
   - Salve a imagem como arquivo e anexe-a na resposta final

RESUMO: {fonte_resumo}
LINK: {fonte_link}

Retorne a legenda e o arquivo da imagem. Não faça perguntas: se faltar
informação, siga com o que está no resumo.
"""

# Formato exigido pela Manus: todo campo em `required` e
# additionalProperties=false.
SCHEMA_SAIDA = {
    "type": "object",
    "properties": {
        "legenda": {
            "type": "string",
            "description": "Legenda final do Instagram, já com as hashtags.",
        },
        "titulo_arte": {
            "type": "string",
            "description": "O título curto que foi escrito na imagem.",
        },
        "nome_arquivo_imagem": {
            "type": "string",
            "description": "Nome do arquivo da imagem anexada (ex.: post.png).",
        },
    },
    "required": ["legenda", "titulo_arte", "nome_arquivo_imagem"],
    "additionalProperties": False,
}


def carregar_marca(caminho: Path | None = None) -> dict:
    if caminho is None:
        caminho = CAMINHO_MARCA_LOCAL if CAMINHO_MARCA_LOCAL.is_file() else CAMINHO_MARCA
    marca = yaml.safe_load(Path(caminho).read_text(encoding="utf-8"))
    marca["max_caracteres"] = min(int(marca["max_caracteres"]), LIMITE_LEGENDA_IG)
    marca["n_hashtags"] = min(int(marca["n_hashtags"]), LIMITE_HASHTAGS_IG)
    return marca


def montar(marca: dict, *, fonte_resumo: str, fonte_link: str) -> str:
    return TEMPLATE.format(
        tom=marca["tom"],
        max_caracteres=marca["max_caracteres"],
        n_hashtags=marca["n_hashtags"],
        descricao_template=" ".join(str(marca["descricao_template"]).split()),
        lado=LADO_IMAGEM,
        fonte_resumo=fonte_resumo.strip(),
        fonte_link=fonte_link.strip(),
    )
