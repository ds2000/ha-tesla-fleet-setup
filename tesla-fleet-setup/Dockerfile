# Global build arg — must be before any FROM to be available in all stages
ARG BUILD_FROM

# Stage 1: Build tesla-http-proxy from source
FROM golang:1.23-alpine AS go-builder

RUN apk add --no-cache git

# Pin to a specific release tag for supply-chain safety
RUN git clone --depth 1 --branch v0.4.1 https://github.com/teslamotors/vehicle-command.git /build

WORKDIR /build

RUN go build -ldflags="-s -w" -o /tesla-http-proxy ./cmd/tesla-http-proxy

# Stage 2: Download cloudflared with version pinning
FROM alpine:3.19 AS cf-downloader

ARG TARGETARCH
RUN apk add --no-cache curl \
    && case "${TARGETARCH}" in \
        amd64)   CF_ARCH="amd64" ;; \
        arm64)   CF_ARCH="arm64" ;; \
        arm*)    CF_ARCH="arm"   ;; \
        *)       CF_ARCH="amd64" ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/2026.3.0/cloudflared-linux-${CF_ARCH}" \
       -o /cloudflared \
    && chmod +x /cloudflared

# Stage 3: Final add-on image
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Copy binaries from builder stages
COPY --from=go-builder /tesla-http-proxy /usr/local/bin/tesla-http-proxy
COPY --from=cf-downloader /cloudflared /usr/local/bin/cloudflared

# Install Python, pip, and certbot (no curl needed at runtime)
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    certbot

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Copy application
COPY rootfs /
COPY screenshots /opt/tesla-setup/static/screenshots/
COPY run.sh /

RUN chmod a+x /run.sh

ENV BUILD_VERSION=${BUILD_VERSION}

CMD ["/run.sh"]
