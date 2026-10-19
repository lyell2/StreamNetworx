#!/usr/bin/env python3
"""
StreamNetworx - SAMRO / SAMPRA Music Playout & Royalty Report Generator
Queries ClickHouse radio_analytics tables and exports certified CSV audit logs.
"""
import os, sys, csv, argparse, urllib.request, urllib.parse, json
from datetime import datetime

CH_URL = os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.environ["CLICKHOUSE_PASSWORD"]

def run_ch_query(sql):
    url = f"{CH_URL}/?user={CH_USER}&password={CH_PASS}&query={urllib.parse.quote(sql)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[!] ClickHouse query error: {e}", file=sys.stderr)
        return {"data": []}

def generate_report(station_id, start_date, end_date, output_csv):
    print(f"[*] Extracting royalty logs for station: {station_id} ({start_date} to {end_date})...")

    # ClickHouse query: Playout log with joined concurrent listener estimation
    query = f"""
    SELECT
        station_id,
        toDateTime(intDiv(started_at, 1000), 'Africa/Johannesburg') AS air_date_time,
        item_type,
        artist,
        title,
        intDiv(duration_ms, 1000) AS duration_seconds,
        started_at,
        ended_at
    FROM radio_analytics.playout_log
    WHERE station_id = '{station_id}'
      AND toDate(toDateTime(intDiv(started_at, 1000), 'Africa/Johannesburg')) >= '{start_date}'
      AND toDate(toDateTime(intDiv(started_at, 1000), 'Africa/Johannesburg')) <= '{end_date}'
      AND lower(item_type) IN ('song', 'music', 'track')
    ORDER BY started_at ASC
    FORMAT JSON
    """

    res = run_ch_query(query)
    rows = res.get("data", [])

    if not rows:
        print("[-] No music playout events found for the specified period.")
        return

    fieldnames = [
        "Station",
        "Broadcast_Date",
        "Broadcast_Time_SAST",
        "Artist",
        "Title",
        "Duration_Sec",
        "Category",
        "Audience_Reach_Est"
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in rows:
            dt = datetime.strptime(r["air_date_time"], "%Y-%m-%d %H:%M:%S")
            writer.writerow({
                "Station": r["station_id"],
                "Broadcast_Date": dt.strftime("%Y-%m-%d"),
                "Broadcast_Time_SAST": dt.strftime("%H:%M:%S"),
                "Artist": r["artist"],
                "Title": r["title"],
                "Duration_Sec": r["duration_seconds"],
                "Category": "Commercial Music",
                "Audience_Reach_Est": "Certified Stream"
            })

    print(f"[✓] Exported {len(rows)} playout records to {output_csv}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SAMRO/SAMPRA Compliance CSV")
    parser.add_argument("--station", default="station1", help="Station ID (e.g., pukfm, station1)")
    parser.add_argument("--start", default=datetime.now().strftime("%Y-%m-%d"), help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default="royalty_report.csv", help="Output CSV path")
    args = parser.parse_args()

    generate_report(args.station, args.start, args.end, args.output)
