FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/gateway

RUN groupadd --gid 10002 gateway \
    && useradd --uid 10002 --gid gateway --create-home --shell /usr/sbin/nologin gateway \
    && install -d -o gateway -g gateway -m 0700 /data

WORKDIR /app

COPY gateway/requirements.txt /app/gateway/requirements.txt
RUN pip install --no-cache-dir -r /app/gateway/requirements.txt

COPY gateway /app/gateway

USER gateway

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "cheby_gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers", "--ws-max-size", "16384", "--ws-max-queue", "4", "--ws-per-message-deflate", "false"]
