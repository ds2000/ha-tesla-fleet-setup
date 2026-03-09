# Global build arg — must be before any FROM to be available in all stages
ARG BUILD_FROM

# Stage 1: Build tesla-http-proxy from source
FROM golang:1.23-alpine AS go-builder

RUN apk add --no-cache git

RUN git clone --depth 1 https://github.com/teslamotors/vehicle-command.git /build

WORKDIR /build

RUN go build -ldflags="-s -w" -o /tesla-http-proxy ./cmd/tesla-http-proxy

# Stage 2: Final add-on image
FROM ${BUILD_FROM}
ARG BUILD_VERSION=dev

# Copy tesla-http-proxy binary from builder
COPY --from=go-builder /tesla-http-proxy /usr/local/bin/tesla-http-proxy

# Install Python, pip, certbot, and UPnP client
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-cryptography \
    certbot \
    miniupnpc \
    curl

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
