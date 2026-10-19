CREATE TABLE IF NOT EXISTS radio_analytics.stream_sessions_raw (
    session_id String,
    node_id LowCardinality(String),
    timestamp_ms Int64,
    started_at_ms Int64 DEFAULT timestamp_ms,
    client_ip String,
    mount LowCardinality(String),
    status UInt16,
    duration Float64,
    bytes_sent UInt64,
    user_agent String,
    listener_id String DEFAULT '',
    billable UInt8 DEFAULT 1,
    geo_country LowCardinality(String) DEFAULT 'XX',
    geo_region String DEFAULT 'Unknown',
    geo_city String DEFAULT 'Unknown',
    asn UInt32 DEFAULT 0,
    asn_org String DEFAULT 'Unknown'
) ENGINE = ReplacingMergeTree()
ORDER BY (node_id, mount, session_id)
TTL toDateTime(timestamp_ms / 1000) + INTERVAL 180 DAY DELETE;
