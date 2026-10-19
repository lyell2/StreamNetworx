CREATE DATABASE IF NOT EXISTS streamnet;

CREATE TABLE IF NOT EXISTS streamnet.customer_stations (
    customer_id String DEFAULT 'UNKNOWN',
    station_name String,
    mountpoint String,
    billing_plan LowCardinality(String) DEFAULT 'standard',
    status LowCardinality(String) DEFAULT 'ONLINE',
    created_at DateTime DEFAULT now(),
    updated_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY mountpoint;

CREATE VIEW IF NOT EXISTS streamnet.streams AS
SELECT mountpoint, status
FROM streamnet.customer_stations;

CREATE TABLE IF NOT EXISTS streamnet.stream_metrics (
    timestamp DateTime,
    node_id LowCardinality(String),
    path LowCardinality(String),
    bytes_received UInt64 DEFAULT 0,
    readers_count UInt32 DEFAULT 0,
    state LowCardinality(String),
    rms_db Float32,
    peak_db Float32,
    bitrate_kbps UInt16
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (path, timestamp, node_id)
TTL timestamp + INTERVAL 180 DAY DELETE;
