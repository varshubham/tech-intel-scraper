FROM python:3.11-slim

# Set INSTALL_PLAYWRIGHT=true to support `type: js` sources (adds ~400 MB):
#   docker build --build-arg INSTALL_PLAYWRIGHT=true -t tech-events .
ARG INSTALL_PLAYWRIGHT=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        pip install "playwright>=1.44,<2.0" && playwright install --with-deps chromium; \
    fi

COPY scraper ./scraper
COPY config ./config
COPY cli.py .

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /app/data \
 && chown -R app:app /app \
 && if [ -d /ms-playwright ]; then chown -R app:app /ms-playwright; fi
USER app

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1

CMD ["python", "cli.py", "serve"]
