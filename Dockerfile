FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    icecast2 \
    ffmpeg \
    python3 \
    python3-pip \
    python3-requests \
    libmaxminddb0 \
    mmdb-bin \
    net-tools \
    iputils-ping \
    nginx \
    libnginx-mod-http-geoip2 \
    iptables \
    wireguard-tools \
    iproute2 \
    jq \
    && rm -rf /var/lib/apt/lists/*

COPY bin/mediamtx /usr/local/bin/mediamtx
COPY bin/vector /usr/local/bin/vector
WORKDIR /app
COPY configs/ /etc/streaming/
COPY scripts/ /app/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /usr/local/bin/mediamtx /usr/local/bin/vector && chmod +x /app/*.py 2>/dev/null || true

EXPOSE 80 443 6000/udp 8000 8080 9997 9998
ENTRYPOINT ["/app/entrypoint.sh"]
