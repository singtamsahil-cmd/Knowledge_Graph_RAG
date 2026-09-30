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

RUN mkdir -p data/input outputs

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
