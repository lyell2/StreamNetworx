# Streaming Radio Infrastructure: Master Project Plan & Implementation Guide

## 1\. Executive Summary & Core Architectural Specifications

This document defines the complete engineering specification, operational contracts, and deployment runbooks for the multi-tenant streaming radio infrastructure. The system provides resilient, low-latency broadcast ingestion via SRT, dynamic multi-node Icecast egress, automated metadata enrichment, and sub-second analytics, containerized into a single multi-role Docker image (`stream-node:latest`).

### 1.1 The Single-FQDN Abstraction Model

The entire multi-server cluster is abstracted behind two canonical endpoints:

- Canonical Ingest Endpoint: `ingest.streammedia.org.za:6000` (SRT over UDP)
- Canonical Egress Endpoint: `stream.streammedia.org.za/live/{station}.mp3` (HTTP/HTTPS over TCP)

Because egress delivery runs in Cloudflare DNS-only mode (Grey Cloud) to maintain raw, unbuffered streaming sockets, Layer 7 proxy routing is bypassed. The system relies on synchronized origin nodes, local health daemons, and internal mesh coordination to maintain high availability.

### 1.2 Network Topology & WireGuard Mesh Addressing

Nodes communicate across a private, full-mesh WireGuard overlay network on the `10.10.0.0/24` subnet:

- `10.10.0.1`: Primary Central Controller & Analytics Hub (ClickHouse, Coordinator State Daemon, Master Clock)
- `10.10.0.2`: Standby Controller (ClickHouse Replica & Secondary Coordinator)
- `10.10.0.10 - 10.10.0.19`: Ingress Fleet (`10.10.0.10`: Ingress-01 JHB, `10.10.0.11`: Ingress-02 CPT)
- `10.10.0.20 - 10.10.0.49`: Egress Delivery Fleet (`10.10.0.20`: Egress-01 JHB, `10.10.0.21`: Egress-02 CPT, `10.10.0.22`: Egress-03 EU)
- `10.10.0.50 - 10.10.0.99`: Internal Infrastructure Services (Prometheus, Grafana, NATS Metadata Bus)

### 1.3 WireGuard MTU & TCP MSS Clamping

WireGuard adds 80 bytes of encapsulation overhead. To prevent IP fragmentation, jitter, and dropped audio packets:

- The WireGuard interface (`wg0`) MTU is locked to `1420 bytes`.
- All nodes enforce TCP Maximum Segment Size (MSS) clamping at the host firewall level: `iptables -t mangle -A POSTROUTING -p tcp --tcp-flags SYN,RST SYN -o wg0 -j TCPMSS --clamp-mss-to-pmtu`
- SRT packet payloads over WireGuard are locked to `1316 bytes` (7 MPEG-TS packets) or `1372 bytes`, guaranteeing zero tunnel-induced fragmentation.
- To prevent cloud NAT tables from dropping idle tunnel state, all peer definitions mandate `PersistentKeepalive = 25`.

### 1.4 Central Coordinator & Failure Mode

The Central Coordinator runs on the Central Hub (`10.10.0.1`) backed by Redis (with key expiration) and a lightweight state daemon:

- It tracks active publisher leases, node registrations, and dynamic egress routing.
- Coordinator Outage Resilience: Ingress and Egress nodes cache active mesh configurations locally. If the Central Hub becomes unreachable over WireGuard, nodes maintain their last-known-good routing topology and continue streaming audio without interruption.

### 1.5 Docker Host Networking & Isolation

All production stream containers run with `network_mode: host`. Internal management APIs bind exclusively to localhost or WireGuard:

- Icecast Audio Server: `127.0.0.1:8000`
- MediaMTX Control API: `127.0.0.1:9997`
- MediaMTX Metrics: `10.10.0.x:9998`
- Vector Syslog Collector: `127.0.0.1:5140` (UDP)
- ClickHouse HTTP/Native: `10.10.0.1:8123` and `10.10.0.1:9000`
- Public Listeners: Only Nginx (Ports 80/443 TCP) and MediaMTX Ingress (Port 6000 UDP) bind to public interfaces.

---

## 2\. Phased Implementation Roadmap

### Phase 1: Network Mesh & Kernel Hardening

- Provision VPS nodes across target datacenters (Hetzner, Netcup).
- Deploy WireGuard full mesh with `1420` MTU, `PersistentKeepalive = 25`, and TCP MSS clamping.
- Apply Linux kernel network tuning and file-descriptor limits (`ulimit -n 65535`).
- Deploy Chrony NTP synchronization to achieve sub-millisecond clock drift across all nodes.

### Phase 2: Ingest Pipeline, Audio Standards & Failover

- Deploy MediaMTX in ingress mode on Ingress nodes (`10.10.0.10`, `10.10.0.11`).
- Configure the Ingest Synthetic Health Daemon on port `8080` (`/healthz`) to monitor UDP port 6000 and MediaMTX process health. Cloudflare Managed Load Balancer monitors this endpoint with a 60-second DNS TTL.
- Enforce the Studio Contribution Audio Standard:
  - Audio Codec: MPEG-1 Audio Layer III (MP3) Constant Bitrate (CBR) strictly. VBR is strictly prohibited.
  - Sample Rate: Exactly 44.1 kHz (44,100 Hz).
  - Channel Format: 2-Channel Stereo or Joint Stereo.
  - Contribution Bitrate: 128 kbps CBR.
  - Loudness Normalization: \-14 LUFS (±1 LUFS) integrated loudness with maximum true peak of \-1.0 dBFS (EBU R128 / ITU-R BS.1770).
  - Studio Encoder Policy: SRT connect timeout set to 3000 ms; mandatory socket teardown and fresh DNS resolution on reconnect; retry backoff of 0s, 2s, up to 5s max.
- Implement publisher collision takeover via central epoch leases and automated upstream socket kicking.

### Phase 3: Mesh Relay, Pre-Warmed Feeds & Fallback Audio

- Deploy Central Node Registry for dynamic egress discovery: Ingress nodes push pre-warmed audio relays to all registered egress nodes over WireGuard.
- Deploy local Icecast daemons on Egress nodes configured with 64 KB burst sizes and fallback mounts.
- Deploy the Persistent Emergency Feeder: A local FFmpeg background loop constantly streams the 15-minute emergency cart into `icecast://127.0.0.1:8000/emergency.mp3`, ensuring the fallback mount is pre-warmed and hot 24/7.
- Implement MediaMTX `runOnReady` dynamic bridges to mount live incoming audio to Icecast via `-c:a copy`.

### Phase 4: Edge Reverse Proxy & Single-FQDN Delivery

- Deploy Nginx reverse proxy on Egress nodes handling HTTP (Port 80\) and HTTPS (Port 443).
- Implement cluster-wide TLS termination: Central Hub acquires Let's Encrypt Wildcard certificates via DNS-01 ACME challenges and synchronizes them to `/etc/ssl/streaming/` across all egress nodes over WireGuard.
- Implement drain-aware `/healthz` endpoint checking `/var/streaming/drain.flag` for Cloudflare Load Balancer de-provisioning.
- Implement client metadata monotonicity rule (`synced_at` and `version` validation) to prevent track title flickering during round-robin DNS polling.
- Embed MaxMind GeoIP2 module, HTML5 audio CORS headers, and ICY UTF-8 character encoding.

### Phase 5: Metadata Enrichment & Distribution Engine

- Deploy central Ingest Metadata Sidecar on port 5000 to receive studio automation webhooks (JSON REST, Myriad OCP XML, GET query strings).
- Implement tiered Spotify API lookup logic: enrich missing artwork/release data while preserving verified studio tags; exclude sweepers/commercials from lookup and history.
- Distribute metadata across the WireGuard mesh using NATS Core under topic `metadata.{station}`.
- Deploy Egress Metadata Agents to write atomic static `nowplaying.json` files and inject ICY title updates into Icecast `/admin/metadata`.

### Phase 6: Telemetry Ingestion & ClickHouse Data Warehouse

- Deploy ClickHouse on the Central Hub (`10.10.0.1`).
- Execute database DDL migrations for raw stream sessions (180-day TTL), playout automation logs, and 1-minute concurrency rollup materialized views.
- Deploy Vector shippers on Egress nodes listening to Nginx in-memory syslog UDP frames (`127.0.0.1:5140`) with 512 MB disk spooling buffers.
- Deploy internal cluster concurrency API querying ClickHouse rollups directly, prohibiting direct scraping of edge Icecast instances.
- Build reporting views for IAB compliance, owned app cohorts, and SAMRO/SAMPRA music royalty returns.

### Phase 7: Access Control, Broadcaster Quotas & Mid-Stream Eviction

- Implement the Broadcaster Suspension Script orchestrating MediaMTX SRT publisher drops and Icecast `killsource` API calls across all egress nodes over WireGuard.
- Configure Nginx `secure_link` HMAC token verification and subscriber concurrency caps (kept dormant on `/premium/` mounts).
- Implement soft billing exemption tagging (`billable = 0`) in the analytics stream.
- Enforce GeoIP country fencing for regionally licensed radio stations.

### Phase 8: Monitoring, Watchdog & Container Packaging

- Build the silence detection watchdog script using FFmpeg `silencedetect` with webhook alerts.
- Build the unified multi-role Dockerfile (`stream-node:latest`) and signal-trapping `entrypoint.sh`.
- Deploy Prometheus exporters and Grafana operational dashboards.
- Execute automated failover verification, simulated encoder drops, and zero-downtime draining tests.

---

## 3\. System Configuration Specifications

### 3.1 Host Kernel, Socket & Network Limits

Add to `/etc/sysctl.d/99-streaming.conf`:

- `fs.file-max = 2097152`
- `net.core.somaxconn = 65535`
- `net.ipv4.tcp_max_syn_backlog = 65535`
- `net.core.rmem_max = 16777216`
- `net.core.wmem_max = 16777216`
- `net.ipv4.tcp_syncookies = 1`
- `net.ipv4.tcp_fin_timeout = 15`

Add to `/etc/security/limits.d/streaming.conf`:

- `* soft nofile 65535`
- `* hard nofile 65535`

---

### 3.2 Unified Multi-Role Dockerfile

Packages all application binaries into a single portable image:

FROM ubuntu:24.04

ENV DEBIAN\_FRONTEND=noninteractive

RUN apt-get update && apt-get install \-y \\

    curl \\

    wget \\

    gnupg \\

    ca-certificates \\

    icecast2 \\

    ffmpeg \\

    python3 \\

    python3-pip \\

    python3-requests \\

    libmaxminddb0 \\

    mmdb-bin \\

    net-tools \\

    iputils-ping \\

    nginx \\

    libnginx-mod-http-geoip2 \\

    iptables \\

    && rm \-rf /var/lib/apt/lists/\*

\# Install pre-packaged binaries from build context

COPY bin/mediamtx /usr/local/bin/mediamtx

COPY bin/vector /usr/local/bin/vector

WORKDIR /app

COPY configs/ /etc/streaming/

COPY scripts/ /app/

COPY entrypoint.sh /app/entrypoint.sh

RUN chmod \+x /app/entrypoint.sh /app/\*.py /usr/local/bin/mediamtx /usr/local/bin/vector

EXPOSE 80 443 6000/udp 8000 8080 9997 9998

ENTRYPOINT \["/app/entrypoint.sh"\]

---

### 3.3 Container Entrypoint Script (`entrypoint.sh`)

Provides process lifecycle management, signal forwarding, and role dispatching:

\#\!/bin/bash

set \-e

ROLE=\${NODE\_ROLE:-combo}

echo "\[INIT\] Booting Stream Node with ROLE: \$ROLE"

ulimit \-n 65535

PIDS=()

shutdown() {

    echo "\[SHUTDOWN\] Intercepted termination signal. Forwarding SIGTERM to all child processes..."

    for PID in "\${PIDS\[@\]}"; do

        if kill \-0 "\$PID" 2\>/dev/null; then

            kill \-TERM "\$PID"

        fi

    done

    wait

    echo "\[SHUTDOWN\] All services stopped cleanly."

    exit 0

}

trap shutdown SIGTERM SIGINT

case "\$ROLE" in

  ingress)

    echo "\[INGRESS\] Starting MediaMTX Ingest..."

    mediamtx /etc/streaming/mediamtx\_ingress.yml &

    PIDS+=(\$\!)

    echo "\[INGRESS\] Starting Synthetic Ingest Health Probe (Port 8080)..."

    python3 /app/ingest\_health\_probe.py &

    PIDS+=(\$\!)

    echo "\[INGRESS\] Starting Ingest Metadata Sidecar..."

    python3 /app/ingest\_metadata\_sidecar.py &

    PIDS+=(\$\!)

    echo "\[INGRESS\] Starting Silence Detection Watchdog..."

    python3 /app/silence\_watchdog.py &

    PIDS+=(\$\!)

    ;;

  egress)

    echo "\[EGRESS\] Starting Icecast Daemon..."

    icecast2 \-c /etc/streaming/icecast.xml &

    PIDS+=(\$\!)

    echo "\[EGRESS\] Starting Persistent Emergency Loop Feeder..."

    python3 /app/emergency\_feeder.py &

    PIDS+=(\$\!)

    echo "\[EGRESS\] Starting MediaMTX Relay..."

    mediamtx /etc/streaming/mediamtx\_egress.yml &

    PIDS+=(\$\!)

    echo "\[EGRESS\] Starting Egress Metadata Consumer..."

    python3 /app/egress\_metadata\_agent.py &

    PIDS+=(\$\!)

    echo "\[EGRESS\] Starting Vector In-Memory Log Shipper..."

    vector \--config /etc/streaming/vector.yaml &

    PIDS+=(\$\!)

    echo "\[EGRESS\] Starting Nginx Proxy..."

    nginx \-g "daemon off;" &

    PIDS+=(\$\!)

    ;;

  combo)

    echo "\[COMBO\] Starting all cluster components..."

    icecast2 \-c /etc/streaming/icecast.xml &

    PIDS+=(\$\!)

    python3 /app/emergency\_feeder.py &

    PIDS+=(\$\!)

    mediamtx /etc/streaming/mediamtx\_combo.yml &

    PIDS+=(\$\!)

    python3 /app/ingest\_health\_probe.py &

    PIDS+=(\$\!)

    python3 /app/ingest\_metadata\_sidecar.py &

    PIDS+=(\$\!)

    python3 /app/egress\_metadata\_agent.py &

    PIDS+=(\$\!)

    python3 /app/silence\_watchdog.py &

    PIDS+=(\$\!)

    vector \--config /etc/streaming/vector.yaml &

    PIDS+=(\$\!)

    nginx \-g "daemon off;" &

    PIDS+=(\$\!)

    ;;

  \*)

    echo "\[ERROR\] Invalid NODE\_ROLE: '\$ROLE'. Use ingress, egress, or combo."

    exit 1

    ;;

esac

wait \-n

---

### 3.4 MediaMTX Configurations

#### Ingress Configuration (`mediamtx_ingress.yml`)

api: yes

apiAddress: 127.0.0.1:9997

metrics: yes

metricsAddress: 10.10.0.10:9998

srt: yes

srtAddress: :6000

\# Central billing and publisher auth webhook

externalAuthenticationURL: "internal-auth-service:5000/api/v1/auth/srt"

paths:

  all:

    source: publisher

    publishUserTimeout: 15s

#### Egress Configuration (`mediamtx_egress.yml`)

api: yes

apiAddress: 127.0.0.1:9997

paths:

  live/\~^(.+)\$:

    source: "srt\_relay\_path"

    sourceOnDemand: no



    \# Bridge MediaMTX audio to local Icecast dynamically

    runOnReady: \>

      ffmpeg \-re \-i "srt\_local\_input\_source"

      \-c:a copy \-content\_type audio/mpeg

      \-f mp3 "icecast\_local\_mount\_destination"

    runOnReadyRestart: yes

---

### 3.5 Icecast Production Configuration (`icecast.xml`)

\<icecast\>

    \<location\>StreamMedia Cluster\</location\>

    \<admin\>tech@streammedia.org.za\</admin\>

    \<limits\>

        \<clients\>10000\</clients\>

        \<sources\>100\</sources\>

        \<burst-size\>65536\</burst-size\>

        \<queue-size\>524288\</queue-size\>

        \<client-timeout\>30\</client-timeout\>

        \<header-timeout\>15\</header-timeout\>

        \<source-timeout\>10\</source-timeout\>

        \<burst-on-connect\>1\</burst-on-connect\>

    \</limits\>

    \<authentication\>

        \<source-password\>__ICECAST_SOURCE_PASS__\</source-password\>

        \<admin-user\>admin\</admin-user\>

        \<admin-password\>__ICECAST_ADMIN_PASS__\</admin-password\>

    \</authentication\>

    \<listen-socket\>

        \<port\>8000\</port\>

        \<bind-address\>127.0.0.1\</bind-address\>

    \</listen-socket\>

    \<\!-- Pre-warmed Fallback Mount Definition \--\>

    \<mount type="default"\>

        \<charset\>UTF-8\</charset\>

        \<fallback-mount\>/emergency.mp3\</fallback-mount\>

        \<fallback-override\>1\</fallback-override\>

        \<no-mount\>0\</no-mount\>

    \</mount\>

    \<mount type="normal"\>

        \<mount-name\>/emergency.mp3\</mount-name\>

        \<charset\>UTF-8\</charset\>

        \<dump-file\>/dev/null\</dump-file\>

    \</mount\>

\</icecast\>

---

### 3.6 Nginx Egress Site Configuration (`nginx.conf`)

Includes the drain-aware health check block, synchronized certificate paths, GeoIP2 enrichment, and in-memory Vector logging:

geoip2 /etc/streaming/GeoLite2-City.mmdb {

    \$geoip2\_data\_country\_code default=XX source=\$remote\_addr country iso\_code;

    \$geoip2\_data\_region\_name default=Unknown source=\$remote\_addr subdivisions 0 names en;

    \$geoip2\_data\_city\_name default=Unknown source=\$remote\_addr city names en;

}

geoip2 /etc/streaming/GeoLite2-ASN.mmdb {

    \$geoip2\_data\_asn default=0 source=\$remote\_addr autonomous\_system\_number;

    \$geoip2\_data\_as\_org default=Unknown source=\$remote\_addr autonomous\_system\_organization;

}

limit\_req\_zone \$binary\_remote\_addr zone=stream\_conn\_rate:10m rate=5r/s;

log\_format vector\_stream\_json escape=json

  '{'

    '"event":"disconnect",'

    '"timestamp\_ms":\$msec,'

    '"node\_id":"\$hostname",'

    '"client\_ip":"\$remote\_addr",'

    '"mount":"\$uri",'

    '"status":\$status,'

    '"bytes\_sent":\$bytes\_sent,'

    '"duration":\$request\_time,'

    '"user\_agent":"\$http\_user\_agent",'

    '"referer":"\$http\_referer",'

    '"ssl\_protocol":"\$ssl\_protocol",'

    '"ssl\_cipher":"\$ssl\_cipher",'

    '"geo\_country":"\$geoip2\_data\_country\_code",'

    '"geo\_region":"\$geoip2\_data\_region\_name",'

    '"geo\_city":"\$geoip2\_data\_city\_name",'

    '"asn":"\$geoip2\_data\_asn",'

    '"asn\_org":"\$geoip2\_data\_as\_org",'

    '"lid":"\$arg\_lid",'

    '"sid":"\$arg\_sid"'

  '}';

upstream icecast\_backend {

    server 127.0.0.1:8000;

}

server {

    listen 80;

    listen 443 ssl reuseport;

    server\_name stream.streammedia.org.za;

    \# Synchronized Wildcard Certificates from Central Hub

    ssl\_certificate /etc/ssl/streaming/fullchain.pem;

    ssl\_certificate\_key /etc/ssl/streaming/privkey.pem;

    \# Universal HTML5 Audio CORS Headers

    add\_header Access-Control-Allow-Origin "\*" always;

    add\_header Access-Control-Allow-Methods "GET, OPTIONS" always;

    add\_header Access-Control-Allow-Headers "Origin, Range, Accept, Icy-MetaData" always;

    \# Drain-Aware Health Probe for Cloudflare Load Balancer

    location /healthz {

        if (-f /var/streaming/drain.flag) {

            return 503 "Draining Node";

        }

        access\_log off;

        proxy\_pass http\://icecast\_backend/status-json.xsl;

    }

    \# Static Monotonic Metadata API File

    location /api/nowplaying.json {

        root /var/www/stream;

        expires 1s;

        add\_header Cache-Control "public, no-transform, max-age=1";

    }

    \# Canonical Audio Mount

    location \~\* ^/live/(.+\\.mp3)\$ {

        limit\_req zone=stream\_conn\_rate burst=10 nodelay;

        access\_log syslog:server=127.0.0.1:5140,facility=local7,tag=stream,severity=info vector\_stream\_json;

        proxy\_pass http\://icecast\_backend;

        proxy\_set\_header Host \$host;

        proxy\_set\_header X-Real-IP \$remote\_addr;

        proxy\_buffering off;

        proxy\_ignore\_client\_abort on;

    }

}

---

### 3.7 Vector Shipper Configuration (`vector.yaml`)

sources:

  nginx\_syslog:

    type: "syslog"

    address: "127.0.0.1:5140"

    mode: "udp"

transforms:

  parse\_json:

    type: "remap"

    inputs: \["nginx\_syslog"\]

    source: |

      . \= parse\_json\!(.message)

      .started\_at \= to\_int\!(.timestamp\_ms) \- to\_int\!(.duration \* 1000\)

sinks:

  clickhouse\_cluster:

    type: "clickhouse"

    inputs: \["parse\_json"\]

    endpoint: "clickhouse-hub:8123"

    database: "radio\_analytics"

    table: "stream\_sessions\_raw"

    skip\_unknown\_fields: true

    batch:

      max\_events: 500

      timeout\_secs: 1

    buffer:

      type: "disk"

      max\_size: 536870912 \# 512 MB emergency spool

      when\_full: "drop\_newest"

---

### 3.8 ClickHouse Analytical Schema & Compliance Views

Execute on ClickHouse Central Hub:

CREATE DATABASE IF NOT EXISTS radio\_analytics;

\-- Raw Sessions Table with 180-Day TTL

CREATE TABLE IF NOT EXISTS radio\_analytics.stream\_sessions\_raw

(

    event LowCardinality(String),

    timestamp\_ms UInt64,

    started\_at UInt64,

    node\_id LowCardinality(String),

    client\_ip String,

    mount LowCardinality(String),

    status UInt16,

    bytes\_sent UInt64,

    duration Float64,

    user\_agent String,

    referer String,

    ssl\_protocol LowCardinality(String),

    ssl\_cipher LowCardinality(String),

    geo\_country LowCardinality(String),

    geo\_region LowCardinality(String),

    geo\_city LowCardinality(String),

    asn UInt32,

    asn\_org LowCardinality(String),

    lid String,

    sid String,

    is\_bot UInt8 DEFAULT match(user\_agent, '(?i)(bot|crawl|spider|slurp|facebook|twitter|pingdom|uptimerobot)'),

    is\_qualified UInt8 DEFAULT (duration \>= 30.0 AND is\_bot \= 0),

    billable UInt8 DEFAULT 1

)

ENGINE \= MergeTree()

PARTITION BY toYYYYMM(toDateTime(timestamp\_ms / 1000))

ORDER BY (mount, timestamp\_ms, client\_ip)

TTL toDateTime(timestamp\_ms / 1000\) \+ INTERVAL 180 DAY DELETE;

\-- Playout Automation Log Table

CREATE TABLE IF NOT EXISTS radio\_analytics.playout\_log

(

    station\_id LowCardinality(String),

    item\_type LowCardinality(String), \-- music, jingle, advert, talk

    artist String,

    title String,

    duration\_ms UInt32,

    started\_at UInt64,

    ended\_at UInt64

)

ENGINE \= MergeTree()

ORDER BY (station\_id, started\_at);

\-- 1-Minute Concurrency Rollup Materialized View

CREATE TABLE IF NOT EXISTS radio\_analytics.concurrency\_1m\_rollup

(

    minute DateTime,

    mount LowCardinality(String),

    total\_sessions UInt32,

    qualified\_sessions UInt32,

    total\_bytes UInt64,

    avg\_duration Float64

)

ENGINE \= SummingMergeTree()

PARTITION BY toYYYYMM(minute)

ORDER BY (mount, minute);

CREATE MATERIALIZED VIEW IF NOT EXISTS radio\_analytics.mv\_concurrency\_1m

TO radio\_analytics.concurrency\_1m\_rollup AS

SELECT

    toStartOfMinute(toDateTime(timestamp\_ms / 1000)) AS minute,

    mount,

    count() AS total\_sessions,

    sum(is\_qualified) AS qualified\_sessions,

    sum(bytes\_sent) AS total\_bytes,

    avg(duration) AS avg\_duration

FROM radio\_analytics.stream\_sessions\_raw

GROUP BY minute, mount;

\-- SAMRO / SAMPRA Monthly Music Royalty Reconciliation View

CREATE VIEW IF NOT EXISTS radio\_analytics.view\_samro\_monthly\_report AS

SELECT

    p.artist AS artist\_name,

    p.title AS track\_title,

    count(DISTINCT p.started\_at) AS total\_plays,

    p.duration\_ms / 1000 AS duration\_seconds,

    count(DISTINCT s.client\_ip) AS total\_reach,

    round(sum(greatest(0, least(s.timestamp\_ms / 1000, p.ended\_at / 1000\) \- greatest(s.started\_at / 1000, p.started\_at / 1000))) / 3600, 2\) AS aggregated\_performance\_hours

FROM radio\_analytics.playout\_log AS p

LEFT JOIN radio\_analytics.stream\_sessions\_raw AS s

    ON s.mount \= concat('/live/', p.station\_id, '.mp3')

   AND s.started\_at / 1000 \<= p.ended\_at / 1000

   AND s.timestamp\_ms / 1000 \>= p.started\_at / 1000

   AND s.is\_qualified \= 1

WHERE p.item\_type \= 'music'

  AND toYYYYMM(toDateTime(p.started\_at / 1000)) \= toYYYYMM(now() \- INTERVAL 1 MONTH)

GROUP BY artist\_name, track\_title, duration\_seconds;

---

## 4\. Automation & Operational Daemons

### 4.1 Ingest Synthetic Health Daemon (`ingest_health_probe.py`)

Monitors UDP port 6000 and MediaMTX process health, serving HTTP 200/503 on port 8080 for Cloudflare Load Balancer DNS withdrawal:

import socket

import subprocess

import json

from http.server import HTTPServer, BaseHTTPRequestHandler

def check\_srt\_listener():

    \# Verify UDP socket binding on port 6000

    try:

        sock \= socket.socket(socket.AF\_INET, socket.SOCK\_DGRAM)

        sock.settimeout(1.0)

        sock.bind(("127.0.0.1", 0))

        sock.close()

    except Exception:

        return False

    \# Check if mediamtx process is active

    try:

        res \= subprocess.run(\["pgrep", "mediamtx"\], stdout=subprocess.PIPE)

        return res.returncode \== 0

    except Exception:

        return False

class HealthHandler(BaseHTTPRequestHandler):

    def do\_GET(self):

        if self.path \== "/healthz":

            if check\_srt\_listener():

                self.send\_response(200)

                self.send\_header("Content-Type", "application/json")

                self.end\_headers()

                self.wfile.write(b'{"status":"healthy","service":"srt-ingress"}')

            else:

                self.send\_response(503)

                self.send\_header("Content-Type", "application/json")

                self.end\_headers()

                self.wfile.write(b'{"status":"unhealthy","error":"srt\_socket\_or\_process\_down"}')

        else:

            self.send\_response(404)

            self.end\_headers()

    def log\_message(self, format, \*args):

        pass \# Suppress standard access logging

if \_\_name\_\_ \== "\_\_main\_\_":

    server \= HTTPServer(("0.0.0.0", 8080), HealthHandler)

    server.serve\_forever()

---

### 4.2 Persistent Emergency Loop Feeder (`emergency_feeder.py`)

Runs on every Egress node to ensure `/emergency.mp3` is continuously fed and always hot:

import subprocess

import time

import os

ICECAST\_URL \= "icecast://source:__ICECAST_SOURCE_PASS__@127.0.0.1:8000/emergency.mp3"

CART\_PATH \= "/etc/streaming/emergency\_loop.mp3"

def stream\_emergency\_loop():

    while True:

        if not os.path.exists(CART\_PATH):

            time.sleep(5)

            continue

        cmd \= \[

            "ffmpeg", "-re",

            "-stream\_loop", "-1",

            "-i", CART\_PATH,

            "-c:a", "copy",

            "-content\_type", "audio/mpeg",

            "-f", "mp3",

            ICECAST\_URL

        \]



        process \= subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        process.wait()

        time.sleep(1) \# Brief pause before auto-restart

if \_\_name\_\_ \== "\_\_main\_\_":

    stream\_emergency\_loop()

---

### 4.3 Silence Detection Watchdog (`silence_watchdog.py`)

Monitors live broadcast audio and triggers alerts when silence persists past 30 seconds:

import subprocess

import re

import requests

import time

ALERT\_HOST \= "central-monitoring:5000"

STREAM\_SRC \= "srt-local:6000?streamid=read:live/pukfm"

def send\_alert(message):

    try:

        url \= "http" \+ "://" \+ ALERT\_HOST \+ "/api/v1/alerts/silence"

        requests.post(url, json={"content": message}, timeout=5)

    except Exception as e:

        print(f"\[WATCHDOG\] Failed to dispatch alert: {e}")

def monitor\_silence():

    cmd \= \[

        "ffmpeg", "-nostats", "-i", STREAM\_SRC,

        "-af", "silencedetect=noise=-50dB:d=30",

        "-f", "null", "-"

    \]



    process \= subprocess.Popen(cmd, stderr=subprocess.PIPE, universal\_newlines=True)

    silence\_start\_pattern \= re.compile(r"silence\_start: (\\d+(\\.\\d+)?)")

    silence\_end\_pattern \= re.compile(r"silence\_end: (\\d+(\\.\\d+)?)")

    for line in process.stderr:

        if "silence\_start" in line:

            match \= silence\_start\_pattern.search(line)

            if match:

                send\_alert("CRITICAL ALERT: Silence detected on live broadcast exceeding 30 seconds.")

        elif "silence\_end" in line:

            match \= silence\_end\_pattern.search(line)

            if match:

                send\_alert("RESOLVED: Audio levels restored on live broadcast.")

if \_\_name\_\_ \== "\_\_main\_\_":

    while True:

        try:

            monitor\_silence()

        except Exception as err:

            time.sleep(10)

---

### 4.4 Real-Time Broadcaster Suspension (`suspend_station.py`)

Executes immediate publisher teardown and listener socket eviction across all nodes over WireGuard:

import requests

import sys

MEDIAMTX\_HOST \= "10.10.0.1:9997"

EGRESS\_NODES \= \["10.10.0.20:8000", "10.10.0.21:8000", "10.10.0.22:8000"\]

ICECAST\_USER \= "admin"

ICECAST\_PASS \= "__ICECAST_ADMIN_PASS__"

def suspend\_stream(station\_slug):

    \# 1\. Sever SRT publisher in MediaMTX

    try:

        url \= "http" \+ "://" \+ MEDIAMTX\_HOST \+ f"/v3/paths/delete/live/{station\_slug}"

        requests.post(url, timeout=3)

    except Exception:

        pass

    \# 2\. Forcibly sever all active listener sockets across all Icecast egress nodes

    for egress\_node in EGRESS\_NODES:

        try:

            url \= "http" \+ "://" \+ egress\_node \+ f"/admin/killsource?mount=/live/{station\_slug}.mp3"

            requests.get(url, auth=(ICECAST\_USER, ICECAST\_PASS), timeout=3)

        except Exception:

            pass

if \_\_name\_\_ \== "\_\_main\_\_":

    if len(sys.argv) \> 1:

        suspend\_stream(sys.argv\[1\])

---

## 5\. Operational Runbooks

### Runbook 1: Adding a New Broadcaster Tenant

1. Provision broadcaster credentials in billing database: assign `station_slug` and API token.
2. Generate modular Icecast mount definition in `/etc/icecast2/mounts.d/{station_slug}.xml` applying plan listener ceiling (`<max-listeners>`).
3. Reload Icecast configuration cleanly: `kill -HUP $(pgrep icecast2)`.
4. Provide the studio engineer with contribution parameters:
   - Ingest Host: `ingest.streammedia.org.za`
   - Port: `6000` (UDP)
   - Stream ID: `publish:{station_slug}:{api_token}`
   - Format: MP3 44.1 kHz, 128 kbps CBR strictly, \-14 LUFS.

### Runbook 2: Performing Zero-Downtime Egress Updates

1. Test Nginx syntax: `nginx -t`.
2. Reload Nginx configuration: `nginx -s reload`. Active long-lived listener sockets continue uninterrupted.
3. Update Icecast XML parameters and issue: `kill -HUP $(pgrep icecast2)`.

### Runbook 3: Investigating Studio Dropouts & Emergency Fallback

1. Inspect silence watchdog alerts to verify if the studio dropped or transmitted silent audio.
2. Verify Icecast status to confirm active transition to `/emergency.mp3`.
3. Check MediaMTX SRT connection state via localhost API.

### Runbook 4: Zero-Downtime Egress Node Draining

1. Step 1 (Activate Drain Flag): On the target egress node, execute: `touch /var/streaming/drain.flag`
2. Step 2 (Probe Verification): Verify `GET /healthz` returns `HTTP 503 Draining Node`.
3. Step 3 (DNS Deprovisioning): Confirm Cloudflare Load Balancer marks the node unhealthy and withdraws its A-record within 10 seconds.
4. Step 4 (Passive Drain Window): Monitor socket decay over 60 to 90 minutes as natural listener turnover occurs: `watch -n 5 "netstat -an | grep :8000 | grep ESTABLISHED | wc -l"`
5. Step 5 (Rolling Eviction): Gently terminate any remaining stuck sockets via Icecast admin `/admin/killsource` over a 60-second randomized window, prompting player apps to reconnect to remaining healthy IPs.
6. Step 6 (Safe Container Stop): Execute `docker stop -t 30 stream-node`. Vector flushes all pending disconnect logs to ClickHouse before container termination.