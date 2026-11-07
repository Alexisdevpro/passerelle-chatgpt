# Python 3.14.10, multiarch manifest verified 2026-11-03.
FROM python:3.14-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GW_HOST=0.0.0.0 GW_PORT=8787 GW_DATA_DIR=/data GW_PROVIDER=chatgpt_oauth
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.lock \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway \
    && mkdir -p /data && chown 10001:10001 /data
RUN chmod 700 /data
COPY --chown=10001:10001 gateway ./gateway
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()"
CMD ["python", "-m", "gateway.cli", "serve"]
