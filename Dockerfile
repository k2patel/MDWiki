# syntax=docker/dockerfile:1.7

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt --target /install

COPY mkdocs.yml ./
COPY hooks ./hooks
COPY overrides ./overrides
COPY admin ./admin
COPY server.py ./server.py
RUN mkdir /content-dir

FROM gcr.io/distroless/python3:nonroot@sha256:1c680cdb442a9e7a89f64fd1706367c62302ea1f9ab80fdebdb72ae9fcded46f

WORKDIR /app
COPY --from=builder --chown=nonroot:nonroot /install /app/site-packages
COPY --from=builder --chown=nonroot:nonroot /build/mkdocs.yml /app/mkdocs.yml
COPY --from=builder --chown=nonroot:nonroot /build/hooks /app/hooks
COPY --from=builder --chown=nonroot:nonroot /build/overrides /app/overrides
COPY --from=builder --chown=nonroot:nonroot /build/admin /app/admin
COPY --from=builder --chown=nonroot:nonroot /build/server.py /app/server.py
COPY --from=builder --chown=nonroot:nonroot /content-dir /data/mdwiki

ENV PORT=8080 \
    MDWIKI_CONTENT_DIR=/data/mdwiki \
    MDWIKI_SITE_DIR=/tmp/mdwiki-site \
    MDWIKI_CONFIG=/app/mkdocs.yml \
    PYTHONPATH=/app/site-packages \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER nonroot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/usr/bin/python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

CMD ["/app/server.py"]
