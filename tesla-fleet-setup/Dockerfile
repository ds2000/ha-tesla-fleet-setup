ARG BUILD_FROM
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Install Python, pip, Go, cloudflared, and build tesla-control
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    curl \
    go \
    git \
    && ARCH="$(apk --print-arch)" \
    && case "$ARCH" in \
        x86_64)  CF_ARCH="amd64" ;; \
        aarch64) CF_ARCH="arm64" ;; \
        armv7l)  CF_ARCH="arm"   ;; \
        *)       CF_ARCH="amd64" ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && CGO_ENABLED=0 go install github.com/teslamotors/vehicle-command/cmd/tesla-control@latest \
    && mv /root/go/bin/tesla-control /usr/local/bin/tesla-control \
    && apk del go git \
    && rm -rf /root/go /root/.cache/go-build /tmp/*

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
