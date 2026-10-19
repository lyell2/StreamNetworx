ALTER TABLE radio_analytics.stream_sessions_raw
    ADD COLUMN IF NOT EXISTS started_at_ms Int64 DEFAULT timestamp_ms,
    ADD COLUMN IF NOT EXISTS listener_id String DEFAULT '',
    ADD COLUMN IF NOT EXISTS billable UInt8 DEFAULT 1,
    ADD COLUMN IF NOT EXISTS geo_country LowCardinality(String) DEFAULT 'XX',
    ADD COLUMN IF NOT EXISTS geo_region String DEFAULT 'Unknown',
    ADD COLUMN IF NOT EXISTS geo_city String DEFAULT 'Unknown',
    ADD COLUMN IF NOT EXISTS asn UInt32 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS asn_org String DEFAULT 'Unknown';

ALTER TABLE radio_analytics.stream_sessions_raw
    MODIFY TTL toDateTime(timestamp_ms / 1000) + INTERVAL 180 DAY DELETE;

CREATE TABLE IF NOT EXISTS radio_analytics.playout_log (
    station_id LowCardinality(String),
    item_type LowCardinality(String),
    artist String,
    title String,
    duration_ms UInt64,
    started_at Int64,
    ended_at Int64
) ENGINE = MergeTree()
ORDER BY (station_id, started_at, title)
TTL toDateTime(started_at / 1000) + INTERVAL 180 DAY DELETE;

CREATE TABLE IF NOT EXISTS radio_analytics.concurrency_1m_active_rollup (
    minute DateTime,
    mount LowCardinality(String),
    geo_country LowCardinality(String),
    total_sessions UInt64,
    qualified_sessions UInt64,
    total_bytes UInt64,
    total_duration Float64
) ENGINE = SummingMergeTree()
ORDER BY (mount, minute, geo_country)
TTL minute + INTERVAL 180 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS radio_analytics.sessions_to_concurrency_1m
TO radio_analytics.concurrency_1m_active_rollup
AS
SELECT
    toDateTime(minute_number * 60) AS minute,
    mount,
    geo_country,
    toUInt64(count()) AS total_sessions,
    toUInt64(countIf(billable = 1)) AS qualified_sessions,
    toUInt64(sumIf(bytes_sent, minute_number = intDiv(timestamp_ms, 60000))) AS total_bytes,
    sum(
        greatest(
            0,
            least(timestamp_ms, (minute_number + 1) * 60000)
                - greatest(started_at_ms, minute_number * 60000)
        )
    ) / 1000.0 AS total_duration
FROM radio_analytics.stream_sessions_raw
ARRAY JOIN range(
    toUInt32(intDiv(started_at_ms, 60000)),
    toUInt32(intDiv(timestamp_ms, 60000)) + 1
) AS minute_number
GROUP BY minute, mount, geo_country;
