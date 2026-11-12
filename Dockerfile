FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PP_LIBRARY=/library \
    PP_DATA=/data \
    PP_PORT=8484

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# TrueNAS SCALE apps run as the "apps" user (568:568) by default.
RUN mkdir -p /data /library && chown 568:568 /data
USER 568:568

EXPOSE 8484
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PP_PORT\"]}/healthz',timeout=4)"

CMD ["python", "-m", "app.main"]
