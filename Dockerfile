FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY tradebot ./tradebot
RUN pip install .

COPY config.example.yaml .env.example ./
VOLUME ["/app/state", "/app/data"]

ENTRYPOINT ["tradebot"]
CMD ["run"]
