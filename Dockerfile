FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Dipendenze Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# File applicativi e asset statici
COPY app.py ./
COPY static/ ./static/

# Cartelle per chiavi EC ES256 e file di configurazione montabile da ./config_ner_demo
RUN mkdir -p /app/keys /app/config_ner_demo && \
    useradd --system --uid 10001 demo && \
    chown -R demo:demo /app

USER demo

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/api/config')" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
