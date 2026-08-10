ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      python -m pip install --no-cache-dir -r requirements.txt; \
    fi

COPY app ./app
COPY config.example.yaml ./config.example.yaml

RUN mkdir -p /config/sessions \
    /media/xydown/raw \
    /media/xydown/library \
    /media/xydown/rebuild \
    /media/xydown/forwarded/raw \
    /media/xydown/forwarded/library

EXPOSE 3434
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3434/api/health', timeout=3)"

CMD ["python", "-m", "app.main"]
