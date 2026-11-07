# syntax=docker/dockerfile:1

ARG CADDY_BUILDER=localhost/caddy-builder
ARG CADDY_RUNTIME=localhost/caddy-runtime
FROM ${CADDY_BUILDER} AS builder

ARG DYNU_VERSION=v1.0.0
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOTOOLCHAIN=local xcaddy build "${CADDY_VERSION}" \
    --with "github.com/caddy-dns/dynu@${DYNU_VERSION}" \
    --output /usr/bin/caddy

FROM ${CADDY_RUNTIME}

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
RUN setcap cap_net_bind_service=+ep /usr/bin/caddy
