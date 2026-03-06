ARG BUILD_FROM

# --- Stage 1: Build tesla-control from source ---
FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS go-builder
ARG TARGETPLATFORM
RUN apk add --no-cache git
RUN case "$TARGETPLATFORM" in \
        "linux/amd64")  export GOARCH=amd64 ;; \
        "linux/arm64")  export GOARCH=arm64 ;; \
        "linux/arm/v7") export GOARCH=arm GOARM=7 ;; \
        "linux/386")    export GOARCH=386 ;; \
        *)              export GOARCH=amd64 ;; \
    esac \
    && CGO_ENABLED=0 GOOS=linux go install github.com/teslamotors/vehicle-command/cmd/tesla-control@latest \
    && cp /go/bin/tesla-control /tesla-control 2>/dev/null \
    || cp /go/bin/linux_*/tesla-control /tesla-control

# --- Stage 2: Final add-on image ---
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Copy tesla-control binary from builder
COPY --from=go-builder /tesla-control /usr/local/bin/tesla-control

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
