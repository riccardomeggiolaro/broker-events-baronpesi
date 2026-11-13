FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Rome

RUN apt-get update \
 && apt-get install -y --no-install-recommends mosquitto supervisor \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --uid 1883 broker

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/supervisord.conf /etc/supervisor/broker.conf
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY app ./app

VOLUME /data
# 8883: MQTT su TLS (in-out)   8080: pannello web (dietro Caddy)
EXPOSE 8883 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,json,sys; s=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz')); sys.exit(0 if s['broker'] else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
