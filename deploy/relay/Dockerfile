FROM python:3.12-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/relay \
    HOME=/home/relay \
    PATH=/usr/local/bin:/usr/bin:/bin

RUN /usr/sbin/addgroup -S -g 10001 relay \
    && /usr/sbin/adduser -S -D -u 10001 -G relay \
        -h /home/relay -s /sbin/nologin relay \
    && install -d -o relay -g relay -m 0700 /data /home/relay

WORKDIR /app

COPY deploy/relay/requirements.lock /app/relay/requirements.lock
RUN pip install --no-cache-dir --require-hashes \
        --requirement /app/relay/requirements.lock

COPY relay/cheby_relay /app/relay/cheby_relay

USER 10001:10001

HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import os,urllib.request; h=os.environ['CHEBY_RELAY_ALLOWED_HOSTS'].split(',')[0]; r=urllib.request.Request('http://127.0.0.1:8080/healthz',headers={'Host':h}); urllib.request.urlopen(r,timeout=2).read()"]

# Keep the transport message ceiling exactly one byte above Relay's 12 MiB
# business ceiling.  This lets Relay emit its deterministic 4409 policy close
# for the first invalid size while the ASGI server hard-closes larger messages.
CMD ["python", "-m", "uvicorn", "cheby_relay.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--log-level", "critical", "--no-access-log", "--no-proxy-headers", "--ws-max-size", "12582913", "--ws-max-queue", "4", "--ws-per-message-deflate", "false"]
