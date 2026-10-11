FROM python:3.12-slim

LABEL org.opencontainers.image.title="sonos-analyzer" \
      org.opencontainers.image.description="Analyze Sonos speakers on your local network, with a web UI" \
      org.opencontainers.image.source="https://github.com/grahamfairbank/sonos-analyzer" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SONOS_ANALYZER_HOST=0.0.0.0 \
    SONOS_ANALYZER_PORT=8400

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sonos_analyzer ./sonos_analyzer
RUN pip install --no-cache-dir .

USER nobody
EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['SONOS_ANALYZER_PORT'], timeout=2)"

CMD ["sonos-analyzer-web"]
