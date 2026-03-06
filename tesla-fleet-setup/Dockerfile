ARG BUILD_FROM
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Install Python, pip, cloudflared, and build tesla-control
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    curl \
    git \
    && ARCH="$(apk --print-arch)" \
    && case "$ARCH" in \
        x86_64)  CF_ARCH="amd64"; GO_ARCH="amd64" ;; \
        aarch64) CF_ARCH="arm64"; GO_ARCH="arm64" ;; \
        armv7l)  CF_ARCH="arm";   GO_ARCH="armv6l" ;; \
        *)       CF_ARCH="amd64"; GO_ARCH="amd64" ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && curl -fsSL "https://go.dev/dl/go1.23.6.linux-${GO_ARCH}.tar.gz" | tar -C /usr/local -xz \
    && CGO_ENABLED=0 /usr/local/go/bin/go install github.com/teslamotors/vehicle-command/cmd/tesla-control@latest \
    && mv /root/go/bin/tesla-control /usr/local/bin/tesla-control \
    && rm -rf /usr/local/go /root/go /root/.cache/go-build /tmp/* \
    && apk del git

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
