# Build Seanime's existing React frontend first, then embed it into one Go binary.
FROM node:22-bookworm-slim AS web-build
WORKDIR /src/seanime-web
COPY seanime-web/ ./
RUN npm ci && npm run build
WORKDIR /src
RUN mkdir -p web && cp -a /src/seanime-web/out/. /src/web/

FROM golang:1.27.1-bookworm AS go-build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
COPY --from=web-build /src/web ./web
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/anime-manager .

FROM debian:bookworm-slim
ARG APP_UID=10001
ARG APP_GID=10001
ENV SEANIME_SERVER_HOST=0.0.0.0 \
    SEANIME_SERVER_PORT=8787 \
    SEANIME_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" appgroup \
    && useradd --uid "${APP_UID}" --gid appgroup --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown -R appuser:appgroup /data
WORKDIR /app
COPY --from=go-build /out/anime-manager /app/anime-manager
USER appuser
VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/app/anime-manager"]
