FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
# Pre-download spaCy model at build time (not every startup)
RUN python -m spacy download en_core_web_lg

COPY src/ ./src/
COPY frontend/ ./frontend/
COPY main.py config.yaml ./
COPY scripts/ ./scripts/

RUN mkdir -p data/input outputs

# Run as non-root in production; /app must stay writable for outputs
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import src.config as c; print('ok')" || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
