# Streaming Architecture Decision Log

## Summary of Decisions

1. **Core Media Routing Engine**

   - **Decision:** Use **MediaMTX** running on lean, unmetered/high-transfer VPS nodes (such as Hetzner/Netcup).
   - **Rationale:** Native support for SRT, RIST, RTMP, WebRTC, and HLS; zero license cost; minimal resource consumption (written in Go); built-in dynamic path creation and cluster peering/relay capabilities. Avoids writing custom socket code.



2. **Dynamic Ingest Topology, Protocol & Failover**

   - **Decision:** Wildcard pathing via **SRT** for multi-node ingestion (`ingest.streammedia.org.za`) paired with free health-checked DNS failover.
   - **Rationale:** Encoders can push to any available node via SRT without pre-defining stream names or static configurations. Using a free health-checked DNS provider (like Cloudflare) automatically drops dead nodes from rotation and triggers automatic encoder reconnection without manual intervention.



3. **Internal Mesh Strategy (Phased: Push Audio, Pull Video)**

   - **Decision:** Deploy a **pre-warmed push/relay mesh for audio (Phase 1\)**, switching to **on-demand pull (`sourceOnDemand`) for video (Phase 2\)** over **WireGuard** private tunnels.
   - **Rationale:**
     - **Audio (Push/Pre-warmed):** A 128 kbps audio stream consumes only \~40–80 GB per month over the internal link, which is negligible on unmetered VPS plans. Pushing audio across all nodes keeps every Icecast mountpoint hot 24/7, eliminating listener connection delays, buffer misses, and the need for complex 404 trigger scripts.
     - **Video (Pull/On-demand):** Multi-Mbps video feeds consume substantial bandwidth and benefit directly from MediaMTX's native sourceOnDemand pulling when viewers request HLS manifests or WebRTC sessions.



4. **Primary Egress Format & Strategy (Phased Rollout)**

   - **Decision:** Prioritize a **constant HTTP connection (.mp3 / Icecast)** for Phase 1 launch, holding HLS in reserve for Phase 2 (mobile app/web caching and video rollout).
   - **Rationale:** Guaranteed compatibility with TuneIn, vTuner, in-car systems, and legacy hardware radios. Provides clean 1:1 TCP session analytics for Concurrent Listeners (CML) and Time Spent Listening (TSL).



5. **MediaMTX to Icecast Dynamic Bridge**

   - **Decision:** Bridge MediaMTX to a local Icecast daemon dynamically using MediaMTX lifecycle hooks (`runOnReady`) with a lightweight FFmpeg source client.
   - **Rationale:** MediaMTX handles the internal transport and SRT ingest but does not speak Icecast's HTTP source mount protocol natively. When an audio stream arrives or is relayed, MediaMTX launches a minimal FFmpeg process (`-c:a copy`) that mounts the audio to Icecast. When the stream stops, MediaMTX terminates the process automatically.



6. **Icecast Fallback Mount & Listener Socket Persistence**

   - **Decision:** Configure Icecast with an internal looping fallback mount (`/silence.mp3` or station backup audio) and `fallback-override: 1`.
   - **Rationale:** Prevents mountpoint destruction and client socket disconnects during temporary encoder reconnects or network jitter. Preserves continuous TCP listener sessions, eliminating fragmented analytics and preventing listener churn on hardware tuners.



7. **Egress and Listener Proxy Layer**

   - **Decision:** Standard HTTP/HTTPS ports (80/443) served via Nginx reverse proxies on egress nodes (`stream.streammedia.org.za`).
   - **Rationale:** Low-overhead delivery shielding backend Icecast daemons and MediaMTX instances, routing audio listener requests to Icecast and future video viewer requests to MediaMTX.



8. **Listener Load Balancing & Steering**

   - **Decision:** Use **Cloudflare Managed Load Balancing (\$5/mo)** in **DNS-only mode (Grey Cloud)** for egress endpoints (`stream.streammedia.org.za`).
   - **Rationale:** Provides zero-maintenance, automated multi-datacenter health checking and failover across egress nodes. Keeps stream delivery direct from origin VPS nodes without passing audio bytes through Cloudflare's proxy (preventing 100-second timeouts or TOS violations). Provides listeners and directories with a single direct URL without redirect hops.



9. **End-to-End Metadata Pipeline (Ingress, Mesh & Egress)**

   - **Decision:** Separate metadata from the audio transport layer. The active ingest node receives metadata (via HTTP API webhook or extracted from in-stream tags) and replicates it across the cluster via a dedicated sidecar service over WireGuard. On each node, a local agent injects updates into local Icecast (`127.0.0.1/admin/metadata`) for ICY delivery and writes a static `nowplaying.json` file served by Nginx.
   - **Rationale:** MediaMTX is strictly a media packet router and lacks an arbitrary key-value replication bus. Using a dedicated sidecar ensures zero coupling between audio encoding stability and track title updates, isolates Icecast admin credentials to localhost, and provides instant, cached REST API access for custom web and app players.



10. **Per-Stream Enrichment Policy & Tiered Metadata Resolution**

    - **Decision:** Implement a stream-aware fallback and enrichment hierarchy at the ingest stage:
      - **Per-Stream Policy Toggle:** Streams configure whether third-party enrichment is enabled (e.g. `streams.pukfm.enrichment: true`, `streams.talk.enrichment: false`).
      - **Tiered Resolution (Encoder \-\> Spotify \-\> Fallback):**
        1. **Encoder-First:** If the encoder/playout provides complete fields (e.g. artist, title, album, custom artwork), those values take top priority.
        2. **Selective Enrichment (Fill-in-the-Blanks):** If enrichment is enabled and the encoder only provides partial fields (e.g., artist and title, but missing album art, release year, or Spotify URL), the ingest service queries Spotify to fill only the missing fields without overwriting verified encoder data.
        3. **Enrichment Disabled / Missing Fields:** If enrichment is disabled or Spotify yields no match, default station branding/artwork is populated.
      - **Replication to Egress APIs:** The resulting unified payload is replicated to all egress nodes, which update their local `nowplaying.json` static API files served by Nginx, ensuring web and mobile apps receive rich, verified metadata identically across the entire cluster.
    - **Rationale:** Gives total control per broadcast stream (avoiding useless lookups on talk shows, sports commentary, or news feeds), avoids blowing through API quotas, and ensures public-facing egress APIs receive full artwork and album metadata even when legacy studio encoders only transmit raw song titles.



11. **Timestamped Precision Synchronization & Rolling In-Memory History**

    - **Decision:** Embed millisecond Unix timestamps (`started_at`, `duration_ms`, `synced_at`) in metadata payloads and maintain a rolling FIFO buffer of recent music tracks directly in `nowplaying.json`.
      - **Timestamp-Based Sync:** Web players and native apps compare the server-provided `started_at` timestamp against their local audio buffer latency, flipping album art and titles on-screen with exact sample alignment rather than relying on clunky server sleep timers.
      - **In-Memory Rolling History:** The ingest node maintains a FIFO array (last 5 to 10 music tracks). When a new track starts, the preceding track moves into the history: \[...\] array. Sweepers and commercial items are omitted from history.
    - **Rationale:** Eliminates arbitrary server-side guessing for buffer delays, enables instant "recently played" visual lists on websites and apps without provisioning a separate database, and keeps edge API delivery completely static and blazing fast.



12. **Bifurcated Audience Analytics & Measurement Architecture**

    - **Decision:** Split analytics pipeline into deterministic identification for owned runtimes and strict IAB-compliant sessionization for blind streams.
      - **Owned Runtimes (Web & Apps):** Pass an anonymous, persistent UUID v4 (`?lid=...`) on stream requests. Groups all connections across IP reassignments and network handovers with 100% certainty.
      - **Blind Streams (Aggregators, In-Car, Smart Speakers):** Bound strictly by IAB/MRC broadcast standards. Enforce a 30-second qualification threshold to drop monitoring bots. On fixed IP connections, multiple sessions within a defined window are de-duplicated into a single daily unique. Cross-network IP handovers on blind streams are counted as separate sessions to eliminate false "Frankenstein" session collisions.
    - **Rationale:** Prevents statistical guessing and false session joining while maximizing data fidelity where code execution exists. Delivers certified, auditable metrics for advertisers and deep retention intelligence for station programmers.



13. **Maximal Edge Telemetry Logging with Late Query-Time Bifurcation**

    - **Decision:** Adopt an ELT (Extract, Load, Transform) data model where edge nodes log exhaustive raw telemetry for every connection, delegating all filtering, bot suppression, and audience segmentation to query-time database views.
      - **Unified Edge Log Format:** Nginx logs 16 distinct attributes on socket close across all platforms: Client IP, Node ID, Mountpoint, HTTP Status, Bytes Sent, Duration (millisecond precision), User-Agent, Referer, TLS Cipher, JA4 TLS Fingerprint, GeoIP Country/Region/City, ISP/ASN, and optional `lid` (Client ID).
      - **Decoupled Reporting Views:** ClickHouse stores the raw stream events verbatim and creates three distinct reporting layers:
        1. **Certified Commercial View:** Automatically filters sessions where `duration < 30` or `is_bot = 1` for sponsor compliance.
        2. **Owned Product Telemetry View:** Aggregates sessions where `lid IS NOT NULL` and joins client-side heartbeat metrics (tab visibility, volume state, buffer underruns).
        3. **Infrastructure & Network Ops View:** Evaluates total raw socket connections, CDN routing behavior, byte egress per node, and drop rates.
    - **Rationale:** Eliminates data loss caused by premature edge filtering, preserves raw forensics for technical debugging, and ensures that differing stakeholder reporting requirements (advertisers vs. app developers vs. broadcast engineers) are satisfied from a single authoritative source of truth.



2. **Dual-Stream Content Attribution & ClickHouse Materialized Rollups**

   - **Decision:** Unify listener telemetry and playout logs inside ClickHouse, fronted by Materialized Views for sub-second dashboard rendering.
     - **Dual-Stream Join:** The active ingest node writes studio automation events (`playout_log`) directly to ClickHouse alongside listener sessions (`stream_sessions`). Queries perform interval-overlap joins to calculate exact commercial reach, song-level listener drop-offs (churn), and presenter shift TSL.
     - **Materialized Views:** Raw socket events are preserved for 180 days, while ClickHouse continuously computes 1-minute concurrency buckets and daily session rollups in the background. Dashboards query pre-aggregated views, keeping UI responsiveness under 15ms.
     - **In-Memory Shippers (Vector):** Edge nodes ship JSON telemetry directly from memory via local Unix sockets using Vector, bypassing disk log files entirely.
   - **Rationale:** Delivers forensic advertising verification without full-table scan penalties, prevents disk I/O bottlenecks during mass disconnect events, and bridges audience consumption with on-air broadcast programming in a single data engine.



3. **Multi-Tenant Ingest Security & Static Gated Mounts**

   - **Decision:** Implement tenant authentication and static paywalling:
     - **Broadcaster Ingest Authentication (MediaMTX `externalAuthenticationURL`):**
       - Encoders pushing SRT streams provide credentials via SRT `streamid` (`publish:station_slug:api_key`).
       - MediaMTX validates incoming publish requests via an internal HTTP webhook to the central billing/auth engine.
     - **Static Paywalling / Gated Mounts:**
       - Paywalled streams use explicit gated mounts (`/premium/{station}.mp3`) with HMAC `secure_link` tokens and concurrency caps. Switching a stream from public to paywalled is treated as a configuration update with a clean reload/restart, avoiding complex in-flight runtime remapping.
     - **Public Anti-Scraping:** CORS origin locks on metadata and Nginx connection handshake rate limits shield edge servers from DDoS or connection storms.
   - **Rationale:** Maintains clean operational boundaries, keeps Nginx configurations simple and deterministic, and avoids unnecessary runtime complexity.



2. **Active Real-Time Suspension on Non-Payment & Billing Controls**

   - **Decision:** Implement immediate, active teardown for non-paying broadcaster streams alongside soft billing exemption toggles:
     - **Active Mid-Stream Eviction (Zero Grace Period on Trigger):**
       - When the billing engine flags a broadcaster account as unpaid/suspended, it MUST NOT wait for an encoder or listener to disconnect naturally.
       - The suspension orchestrator immediately executes a two-step kill across the cluster:
         1. **Sever Publisher at Ingest:** Calls MediaMTX control API (`POST /v3/srtconns/kick` or path disconnect) to forcibly drop the studio's active SRT connection. The central auth webhook revokes the key so subsequent connection handshakes are immediately rejected (403).
         2. **Forcibly Eject Active Listeners at Egress:** Calls Icecast admin API (`GET /admin/killsource?mount=/live/{station}.mp3`) across all egress nodes over WireGuard. This terminates all open listener TCP sockets in milliseconds, immediately halting outbound data transfer and stopping further egress bandwidth accumulation.
     - **Soft Billing Exemption (Courtesy / Testing Mode):**
       - Allows live streams to remain active without racking up customer invoices by tagging sessions with `billable = 0` in ClickHouse.
   - **Rationale:** Prevents delinquent broadcaster streams from continuing to run indefinitely on long-lived connections, stops hosting bandwidth costs immediately upon non-payment, and provides clean operational control over billing state.



3. **Broadcaster Plan Quotas, Geographic Licensing Enforcement & Leech Protection**

   - **Decision:** Establish automated plan quotas, territorial rights enforcement, and anti-leeching mechanisms across the edge layer:
     - **Tiered Capacity Ceilings:**
       - Each broadcaster's plan defines a peak concurrent listener limit (`max-listeners` inside Icecast mount configurations, e.g. 500 peak listeners) and an egress bandwidth quota.
       - The analytics engine tracks monthly usage against quotas, generating automated alerts at 80% and 95% before triggering grace-period overage billing or suspension workflows.
     - **Geographic Licensing & Regulatory Fencing:**
       - Nginx checks client country codes via GeoIP2 against per-station policy whitelists or blacklists.
       - Enables radio stations holding territorial-only digital streaming licenses (such as South African domestic rights under SAMRO/SAMPRA, or geoblocked regional sports rights) to restrict playback strictly to authorized territories, returning HTTP 403 or routing unauthorized international listeners to a licensing slate.
     - **Anti-Leech & Stream Theft Protection:**
       - Icecast admin interfaces are strictly isolated to loopback (`127.0.0.1`) and private WireGuard interfaces, completely shielding server administration and raw mount status from public access.
       - Unauthorized third parties attempting to scrape and re-stream radio mounts as their own source feeds are thwarted via Nginx User-Agent blocks and referer constraints where applicable.
     - **Self-Service Credential Rotation:**
       - Broadcasters can rotate their SRT publishing secrets instantly via their control panel. Invalidation triggers the active mid-stream eviction sequence, forcing studio encoders to reconnect using updated credentials.
   - **Rationale:** Prevents plan resource abuse, guarantees statutory music licensing compliance for territorial broadcasters, stops third-party stream hijacking, and provides self-service security management for station technical personnel.



4. **Cluster Health Telemetry, Silence Detection & Alerting Watchdog**

   - **Decision:** Deploy a lightweight, multi-layered monitoring stack spanning infrastructure health, synthetic stream probes, and audio silence detection:
     - **Silence & Dead-Air Detection:** Ingest nodes run an automated watchdog using FFmpeg `silencedetect` (`noise=-50dB:d=30`). If audio levels stay under \-50 dB for 30 consecutive seconds on an active SRT feed, an alert webhook triggers immediately (Telegram/Discord/Opsgenie).
     - **Synthetic Egress Probing:** Cloudflare Load Balancer and internal Prometheus Blackbox Exporters probe `/healthz` on each egress node every 10 seconds.
     - **Metrics Pipeline:** Node Exporter, MediaMTX native metrics (`:9998/metrics`), Icecast exporter, and Vector process stats are scraped by a central Prometheus instance and visualized in Grafana.
   - **Rationale:** Catches studio encoder freezes, muted lines, and node degradation before listeners report outages, and provides end-to-end operational visibility.



5. **Unified Single-Image Multi-Role Docker Container Architecture**

   - **Decision:** Package the entire software suite into a single, multi-role Docker container image (`stream-node:latest`) orchestrated by a lean process manager (such as s6-overlay or a role-based entrypoint script), parameterized via a single environment variable: `NODE_ROLE=ingress|egress|combo`.
     - **Ingress-Only Role (`NODE_ROLE=ingress`):** Boots MediaMTX (configured for SRT ingest & authentication webhooks), metadata ingest sidecar (Spotify enricher & WireGuard fan-out), silence detection watchdog, and Prometheus metrics. Does NOT boot Icecast or Nginx.
     - **Egress-Only Role (`NODE_ROLE=egress`):** Boots MediaMTX (mesh listener/relay), local Icecast daemon, FFmpeg dynamic bridge, Nginx reverse proxy with GeoIP2, Vector log shipper, and metadata edge consumer.
     - **Combo Role (`NODE_ROLE=combo` / `all`):** Boots the entire stack inside a single container for single-server setups, staging environments, or testing.
   - **Rationale:** Dramatically accelerates node deployment to a single `docker run` command, maintains 100% dependency consistency across all VPS providers, avoids container sprawl, and enables rapid scaling of new edge nodes in under two minutes.



6. **Operational Edge Resilience, Directory Hooks & Buffer Tuning**

   - **Decision:** Standardize production audio buffer defaults, external directory sync, and edge resilience protocols:
     - **Emergency Branded Loop:** Replace fallback `/silence.mp3` with `/emergency.mp3` containing a 15-minute looping audio bed with station sweepers and technical notice voiceovers. Prevents car head units from auto-seeking and listeners from abandoning streams during encoder reconnects.
     - **Instant-Play Buffer Tuning:** Configure Icecast `burst-size` to 64 KB (or 128 KB) and `queue-size` to 512 KB, reducing player startup latency to under 200ms without buffering stutter.
     - **Directory Push (TuneIn AIR API):** Ingest sidecar pushes real-time track updates directly to TuneIn's Broadcaster API on track transitions.
     - **Vector Spooling Backpressure:** Configure Vector sinks with a 512 MB disk buffer spool to survive database maintenance or WireGuard network interruptions without dropping audience telemetry.
     - **Automated Data Lifecycle:** Enforce a 180-day TTL on ClickHouse raw session tables to automatically reclaim disk space while retaining aggregated metrics indefinitely.
     - **HTML5 Web Audio CORS Compliance:** Serve stream endpoints with permissive CORS headers to allow browser canvas visualizers to access audio byte arrays.
     - **Zero-Downtime Reloads:** Enforce graceful reloads (`nginx -s reload`, `kill -HUP` on Icecast, MediaMTX auto-reload) for tenant configuration updates without dropping active listeners.
   - **Rationale:** Delivers seamless listener experience on bad cellular connections, maximizes station visibility on directory platforms, protects server disk resources, and guarantees continuous on-air uptime during administration.