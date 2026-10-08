# Optional central server (plan item 19): the full OMR server with current
# Python, Tesseract (English + Hindi/Devanagari), ONNX Runtime and every export.
# For a site with one newer machine (Windows 10/11 with Docker Desktop, or a
# Linux server). Windows 7 PCs do not run Docker; they open this server in
# Chrome 109 or Firefox ESR 115. See packaging/README.md, "Docker server".
#
#   docker compose up -d            # then open http://<server>:8000/
#
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMR_HOST=0.0.0.0 \
    OMR_PORT=8000 \
    OMR_DATA_DIR=/data \
    MPLCONFIGDIR=/tmp/matplotlib

# Tesseract engine + language data; libzbar0 for the optional pyzbar engine.
# The -dev packages and compiler build tesserocr (in-process Tesseract); the
# compiler is removed again afterwards.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin libzbar0 libglib2.0-0 \
        libtesseract-dev libleptonica-dev pkg-config g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.api.txt ./
RUN pip install -r requirements.api.txt pyzbar \
 && apt-get purge -y --auto-remove pkg-config g++

COPY main.py ./
COPY src ./src
COPY samples ./samples
COPY web/omr-browser ./web/omr-browser

# Optional learned models (bubble / ICR / PaddleOCR ONNX files): mount them at
# /app/models (see docker-compose.yml) and point config.json ml_params at them.
RUN mkdir -p /data /app/models && useradd --system --home /app omr \
 && chown -R omr /data /app/models
USER omr
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0)"

CMD ["python", "-m", "src.api"]
