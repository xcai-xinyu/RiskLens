FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
    ANONYMIZED_TELEMETRY=False LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates util-linux git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY api/ api/
COPY frontend/ frontend/
COPY main.py ./
COPY evaluation/repository.py evaluation/
COPY tests/fixtures/demo_v1.json tests/fixtures/p1_model_v1.json tests/fixtures/p2_semantic_v1.json tests/fixtures/
COPY tests/fixtures/regression/p2_adversarial_v1.json tests/fixtures/regression/p2_adversarial_v2.json tests/fixtures/regression/
COPY data/sample/corpus_v1.json data/sample/
COPY scripts/fetch_demo_source.py scripts/p5_smoke.py scripts/
# Fetch public source by its existing frozen hash; no private corpus/index copied.
RUN python scripts/fetch_demo_source.py \
    && useradd --create-home --uid 10001 risklens \
    && mkdir -p data/private data/documents \
    && chown -R risklens:risklens data
USER risklens
EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
