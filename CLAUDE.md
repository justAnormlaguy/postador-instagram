# Postador

Como funciona, decisões de design e onde diverge do plano original: `README.md`.

- Python 3.10+, roda no i3 do homelab ao lado do curador-noturno (mesmo usuário, systemd).
- Toda troca de status passa por `postador/fila/cliente.py::mudar_status` (compare-and-swap);
  transições permitidas só em `postador/fila/tipos.py`. Nunca apague linhas da fila.
- O publicador (`postador/publicador/`) é o único módulo que muda entre Fase 1 (humano) e Fase 2 (Graph API).
- Antes de commitar: `python eval/teste_fumaca.py` precisa terminar com `TUDO OK ✓`.
- Docs da Manus API v2: https://open.manus.ai/docs/llms.txt (spec OpenAPI em /docs/v2/openapi_v2.json).
