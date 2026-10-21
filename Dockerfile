# Image du bot et du site (même image, deux commandes). Python 3.12, utilisateur non-root.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Paris

WORKDIR /app
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin daybot

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY daybot ./daybot
COPY config.toml ./config.toml
RUN mkdir -p /app/data && chown daybot:daybot /app/data

USER daybot
VOLUME ["/app/data"]

# Le bot écrit data/heartbeat chaque minute : sans signe de vie depuis 5 min → « unhealthy ».
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD python -c "import os,sys,time; p='/app/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 300 else 1)"

CMD ["python", "-m", "daybot", "--config", "/app/config.toml", "run"]
