# syntax=docker/dockerfile:1

# ---------- runtime image ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf-cache \
    PYTHONPATH=/app/src

# git: needed to ingest a GitHub repository URL (shallow clone).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only PyTorch: ~10x smaller than the default CUDA build.
RUN pip install torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model into the image so the server never downloads at runtime
# (first tool call would otherwise hang/time out, and it must work without network).
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY src/ ./src/
# Eval set + benchmark/eval scripts, so they run with `docker compose run` (no mounts).
COPY scripts/ ./scripts/
COPY eval/ ./eval/

RUN useradd --create-home --uid 1000 app && chown -R app /opt/hf-cache
USER app

ENTRYPOINT ["python", "-m", "rag_mcp"]
CMD ["serve"]

# ---------- test image (runtime + pytest + tests) ----------
FROM runtime AS test
USER root
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests/ ./tests/
USER app
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]
