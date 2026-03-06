ARG BUILD_FROM

# --- Build tesla-control with Go 1.23 ---
FROM golang:1.23-alpine AS go-builder
RUN apk add --no-cache git \
    && git clone --depth 1 https://github.com/teslamotors/vehicle-command.git /src \
    && cd /src \
    && CGO_ENABLED=0 go build -o /go/bin/tesla-control ./cmd/tesla-control

# --- Final add-on image ---
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Copy pre-built tesla-control
COPY --from=go-builder /go/bin/tesla-control /usr/local/bin/tesla-control

# Install Python, pip, and cloudflared
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    curl \
    && ARCH="$(apk --print-arch)" \
    && case "$ARCH" in \
        x86_64)  CF_ARCH="amd64" ;; \
        aarch64) CF_ARCH="arm64" ;; \
        armv7l)  CF_ARCH="arm"   ;; \
        *)       CF_ARCH="amd64" ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

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
