FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --gid 10001 webhook && useradd --uid 10001 --gid webhook --no-create-home webhook && mkdir /data && chown webhook:webhook /data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=webhook:webhook . .
USER webhook
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["sh", "-c", "flask --app wsgi db upgrade && exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 60 --access-logfile - wsgi:app"]
