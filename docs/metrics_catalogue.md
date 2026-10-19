# StreamNetworx: Master Metrics Catalog

This document serves as the master reference for all metrics collected across the StreamNetworx SaaS platform, categorized by consumer and collection method.

## Tier 1: Infrastructure & Delivery (The SaaS Layer)

*Captured entirely server-side. Used to monitor node health, optimize delivery, and calculate billing.*

* **Total Egress (GB)**
* **Average Bitrate Served (kbps)**
* **Peak Active Sockets**
* **Time-to-First-Byte (TTFB)**
* **HTTP Error Rate**
* **Edge Node Distribution**
* **Disconnect Reason**
* **TLS/SSL Fingerprinting (JA3/JA3S):** Unmasks bots, stream-rippers, and generic scrapers pretending to be mobile browsers based on their SSL handshake.
* **TCP Slow Reader / Zero Window Rate:** Identifies listeners whose network is so poor they are buffering at the TCP socket layer, causing backend strain.
* **Protocol Distribution:** Tracks how much traffic is riding over HTTP/1.1 vs HTTP/2 vs HTTP/3 (QUIC) for latency optimization.
* **Unauthorized Token Rejections (401/403):** Spikes in failed connections from expired tokens, invalid signatures, or unauthorized hotlinking attempts.
* **Internal Proxy Latency:** The microsecond delay between the edge proxy requesting the stream and Icecast serving it locally.

## Tier 2: Audience & Reach (The Business Layer)

*Industry-standard metrics to prove reach. Captured server-side and enriched with GeoIP/User-Agent data.*

* **Current Concurrents**
* **Time Spent Listening (TSL) / Duration**
* **Average Minute Audience (AMA)**
* **Cumulative Audience (Cume)**
* **Bounce Rate**
* **Geolocation**
* **Network Type & ISP**
* **Device Category**
* **Authenticated Listener UUID:** The holy grail metric. Maps the proxy-validated token directly to a known user profile, completely eliminating the inaccuracies of Carrier-Grade NAT (CGNAT) where hundreds of mobile listeners share one IP address.
* **Multi-Device Concurrency:** Flags when a single authenticated user/token is streaming across multiple devices simultaneously (e.g., left it running on a desktop while listening on a phone).
* **Granular Session Drop Reason:** Distinguishes a graceful client exit (TCP FIN) from a sudden loss of cellular signal (TCP RST or Timeout).
* **Referer & Origin Hotlinking:** Explicit tracking of exactly which external websites or third-party directories are embedding your stream URL without authorization.

## Tier 3: Client QoS & Behavior (The Application Layer)

*Native Only metrics requiring the app or web player telemetry.*

* **Time-to-First-Audio (TTFA)**
* **Buffer Underrun Rate**
* **Stream Restarts**
* **Listening Environment / Output**
* **Foreground vs. Background TSL**
* **Play / Pause / Stop Intents**
* **Volume & Mute Events**
* **App Version & Build Tracking:** Ingests the exact over-the-air patch or app version the listener is using to correlate bug reports with stream drops.
* **Wake-Lock / Battery Optimization Kills:** Tracks when the mobile OS forcibly terminates the background audio service.
* **Cellular vs. Wi-Fi Transitions:** Telemetry confirming when a stream seamlessly hands off between network interfaces without dropping the proxy socket.

## Tier 4: Monetization & Ad Operations (The Revenue Layer)

*Critical for programmatic audio ads and Server-Side Ad Insertion (SSAI).*

* **Client-Confirmed Ad Play**
* **Ad Quartile Completion**
* **Ad Fill Rate**
* **Ad Skip / Drop-Off Rate**
* **SSAI Impression Stitching Match:** Cryptographically verifies that the TCP socket receiving the ad block belongs to the exact token that requested it, preventing ad-fraud.
* **Ad Payload Wasted Bandwidth:** Calculates the data cost of streaming commercial blocks to listeners who immediately muted or backgrounded the app.

## Tier 5: Operational & Financial Health (The Platform Layer)

*Used by administrators to evaluate scaling efficiency and profitability.*

* **Cost Per Listener Hour (CPLH)**
* **Ingest-to-Edge Latency**
* **Cache Hit Ratio**
* **Node CPU/Memory Utilization**
* **Referrer / UTM Origin**
* **Bandwidth Cost per Tenant:** Granular allocation of transit costs per station based on strict token usage.
* **Orphaned Socket Drain:** Tracks connections where the user paused the player but the socket stayed open, silently bleeding bandwidth (which the proxy can be programmed to forcefully sever).