# For prod you'd want to pin the version: e.g., 2.9.1-builder
FROM caddy:builder AS builder

ARG DYNU_VERSION=v1.0.0
RUN xcaddy build \
    --with "github.com/caddy-dns/dynu@${DYNU_VERSION}"
FROM caddy:latest

COPY --from=builder /usr/bin/caddy /usr/bin/caddy