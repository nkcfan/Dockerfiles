ARG CADDY_VERSION=2.11.7
FROM caddy:${CADDY_VERSION}-builder AS builder

ARG CADDY_VERSION
ARG DYNU_VERSION=v1.0.0
RUN xcaddy build "v${CADDY_VERSION}" \
    --with "github.com/caddy-dns/dynu@${DYNU_VERSION}"
FROM caddy:${CADDY_VERSION}

COPY --from=builder /usr/bin/caddy /usr/bin/caddy