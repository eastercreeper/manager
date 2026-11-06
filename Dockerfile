FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
ARG APP_UID=10001
ARG APP_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini && rm -rf /var/lib/apt/lists/* && \
    groupadd --gid "${APP_GID}" appgroup && \
    useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN mkdir -p /data /downloads/anime /ivan/Shows && chown -R appuser:appgroup /app /data /downloads/anime
USER appuser
EXPOSE 8787
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
