# Single standalone Anime Manager image. Seanime is the embedded Go application
# foundation; no second Seanime service is launched.
FROM golang:1.27.1-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/anime-manager .

FROM debian:bookworm-slim
ENV SEANIME_SERVER_HOST=0.0.0.0 \
    SEANIME_SERVER_PORT=8787 \
    SEANIME_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 appgroup \
    && useradd --uid 10001 --gid appgroup --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown -R appuser:appgroup /data
WORKDIR /app
COPY --from=build /out/anime-manager /app/anime-manager
USER appuser
VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/app/anime-manager"]
