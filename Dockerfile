# Backend image. IMPORTANT: build with the REPO ROOT as context, not this
# folder, because the backend imports coding_agent.memory.kg from ../agent:
#     docker build -f visualisation/backend/Dockerfile -t kg-viz-backend .
# (the docker-compose.yml in visualisation/ already sets this context.)
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

# deps first for layer caching
COPY visualisation/backend/requirements.txt .
# requirements.txt installs under it (-c), so it must sit beside it.
COPY visualisation/backend/langgraph-constraints.txt .
# pip reads PIP_INDEX_URL from the environment; unset, it uses PyPI as before.
ARG PIP_INDEX_URL
# Refuse a mirror argument carrying credentials (user:token@): a build
# argument's value is kept in the image history. Prints the name, never the value.
RUN for a in PIP_INDEX_URL; do eval "v=\${$a-}"; case "$v" in *://*@*) echo "refused: build argument $a has credentials in its URL, and build arguments are kept in the image history" >&2; exit 1;; esac; done
RUN pip install --no-cache-dir -r requirements.txt

# Every backend module, by glob rather than by name. Naming them individually
# meant a new module (docstore_view.py) shipped an app.py that imported it and a
# container that crash-looped on ModuleNotFoundError — the enumeration itself was
# the bug. The glob is FLAT (it skips subdirectories), which is why the backend
# modules stay flat here and tests/ is excluded for free — if you ever nest a
# runtime module, add its own COPY like conversations/ gets below.
COPY visualisation/backend/*.py .

# The conversation API package (spec 2026-09-21-p2a). A separate COPY because
# the glob above is FLAT and silently skips subdirectories - without this the
# `from conversations import routes` import in app.py fails closed, and
# /api/conversations/* is never mounted.
COPY visualisation/backend/conversations/*.py ./conversations/

# The agent package, WHOLE. It used to be copied file by file ("just what the
# backend imports"), and that enumeration broke the viewer twice: a module both
# lists forgot, and - worse - a package copied only PARTLY (pbi/ with estate/
# alone), which shadows the full package and cannot be rescued by anything
# (final review C1, 2026-09-19: the Source & Target screen's import of
# coding_agent.pbi.contracts took the whole backend down). The source is ~2.6 MB
# and stdlib-first; what keeps the image slim is requirements.txt, not this
# COPY - a module that needs a heavy dependency still fails to import here, and
# tests/test_image_import_layout.py rebuilds THIS layout from these COPY lines
# and imports everything connections.py and app.py reach. app.py adds /agent to
# sys.path, matching the ../agent layout it expects at runtime.
COPY agent/coding_agent /agent/coding_agent

# The whole macquarie package, as the combined image copies it (visualisation/
# Dockerfile: COPY agent/macquarie /agent-src/macquarie). conversations/runner.py
# compiles the front door in-process, and the skills read governance records by
# path under the directory `macquarie` imports from: the promotion indexes, every
# report's rule record, run ledger and reportability record, connections.toml,
# the GA1 ledger. This used to be a per-file allow-list, and it stopped at
# ARF 223.0: a report onboarded later (ARF 220.0, APG 223) had its register
# published in the KG but its records missing here, so nl_query listed it and then
# failed on its rule record (2026-09-22). Compose overlays skills/ with the local
# directory for development. MIGRATION_LEDGER / CONNECTIONS_CONFIG still override
# the two Source & Target paths.
COPY agent/macquarie /agent/macquarie

# BP11: the form_registration capability runs Phase A of the onboarding runbook as
# subprocesses of these four scripts, unchanged; they import only each other and
# coding_agent. An allow-list, never the whole scripts/ folder: four KG scripts have
# no argparse, so even --help executes them (kg_reseed reseeds), and this image is
# web-facing. Not in the combined image: on Azure the capability refuses before it
# would run one. tests/test_image_import_layout.py pins the list and its imports.
COPY agent/scripts/apra_acquire.py agent/scripts/apra_retrieval_contract.py \
     agent/scripts/apra_classification_draft.py agent/scripts/apra_ingest.py /agent/scripts/

# BP12, no outbound in the bank: no langgraph CLI analytics, no LangSmith traces
# by default. Run-time env (compose `environment:`, Azure settings) still wins.
# Never set *_TRACING_V2 here: langsmith reads it first, so it would beat .env's
# LANGSMITH_TRACING=true. tests/test_image_no_outbound.py pins this.
ENV LANGGRAPH_CLI_NO_ANALYTICS=1 LANGSMITH_TRACING=false
EXPOSE 8000
# 0.0.0.0 so the port is reachable from outside the container
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
