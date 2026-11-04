# Stage 1: builder
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY alembic.ini ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir build \
    && pip wheel --no-cache-dir --wheel-dir /dist -e .

# Stage 2: runtime
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid 1001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appgroup /app/logs

COPY --from=builder /dist /dist
COPY alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY config ./config
COPY models ./models
COPY static ./static
COPY scripts ./scripts
COPY check_collection_status.py ./
COPY delete_collection.py ./

RUN pip install --no-cache-dir /dist/*.whl

USER appuser

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
