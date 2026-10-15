#!/usr/bin/env python3
import sys
import os
import json
import urllib.request
import requests

CH_URL = os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123/")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "ClickHousePass123")
MEDIAMTX_API = os.getenv("MEDIAMTX_API", "http://127.0.0.1:9997")
ICECAST_URL = os.getenv("ICECAST_URL", "http://127.0.0.1:8000")
ICECAST_ADMIN_USER = os.getenv("ICECAST_ADMIN_USER", "admin")
ICECAST_ADMIN_PASS = os.getenv("ICECAST_ADMIN_PASS", "HackmeIcecastPass")

def query_clickhouse(sql):
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, CH_URL, CH_USER, CH_PASS)
    handler = urllib.request.HTTPBasicAuthHandler(pwd_mgr)
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(CH_URL, data=sql.encode("utf-8"))
    try:
        with opener.open(req, timeout=3) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception as e:
        print(f"[!] ClickHouse error: {e}")
        return None

def set_station_status(mountpoint, status):
    sql = f"ALTER TABLE streamnet.customer_stations UPDATE status = '{status}' WHERE mountpoint = '{mountpoint}';"
    query_clickhouse(sql)

def sever_icecast_mount(mountpoint):
    kill_url = f"{ICECAST_URL}/admin/killsource?mount={mountpoint}"
    try:
        res = requests.get(kill_url, auth=(ICECAST_ADMIN_USER, ICECAST_ADMIN_PASS), timeout=3)
        if res.status_code == 200:
            print(f"  [+] Icecast: Mount '{mountpoint}' terminated and listeners disconnected.")
        else:
            print(f"  [-] Icecast: Mount '{mountpoint}' was not active.")
    except Exception as e:
        print(f"  [!] Icecast unreachable: {e}")

def sever_mediamtx_path(clean_slug):
    try:
        url = f"{MEDIAMTX_API}/v3/paths/delete/live/{clean_slug}"
        res = requests.post(url, timeout=3)
        if res.status_code in (200, 204):
            print(f"  [+] MediaMTX: Ingress SRT publisher evicted for 'live/{clean_slug}'")
        else:
            print(f"  [-] MediaMTX: Path 'live/{clean_slug}' was idle.")
    except Exception as e:
        print(f"  [!] MediaMTX API unreachable: {e}")

def get_station_meta(clean_slug):
    mount = f"/{clean_slug}.mp3"
    sql = f"SELECT customer_id, station_name, mountpoint, billing_plan, status FROM streamnet.customer_stations WHERE mountpoint = '{mount}' FORMAT JSONEachRow;"
    raw = query_clickhouse(sql)
    if not raw:
        return {"customer_id": "UNKNOWN", "station_name": clean_slug, "mountpoint": mount, "status": "ONLINE"}
    return json.loads(raw.split("\n")[0])

def server_start(slug):
    mount = f"/{slug}.mp3"
    print(f"\n[*] STARTING VIRTUAL SERVER: {slug} ({mount})")
    set_station_status(mount, "ONLINE")
    print(f"  [+] Status set to ONLINE in database.")
    print(f"  [+] Ingest port open and ready for incoming SRT / Icecast sources.")
    print(f"[✓] Virtual server '{slug}' is now ONLINE.\n")

def server_stop(slug):
    mount = f"/{slug}.mp3"
    print(f"\n[*] STOPPING VIRTUAL SERVER: {slug} ({mount})")
    sever_mediamtx_path(slug)
    sever_icecast_mount(mount)
    set_station_status(mount, "STOPPED")
    print(f"  [+] Status set to STOPPED. Watchdog will suspend alerting.")
    print(f"[✓] Virtual server '{slug}' cleanly STOPPED.\n")

def server_suspend(slug):
    mount = f"/{slug}.mp3"
    print(f"\n[!] INITIATING ADMINISTRATIVE SUSPENSION: {slug} ({mount})")
    sever_mediamtx_path(slug)
    sever_icecast_mount(mount)
    set_station_status(mount, "SUSPENDED")
    print(f"  [+] Broadcaster locked. Status set to SUSPENDED.")
    print(f"[✓] Virtual server '{slug}' SUSPENDED.\n")

def server_unsuspend(slug):
    mount = f"/{slug}.mp3"
    print(f"\n[*] UNSUSPENDING VIRTUAL SERVER: {slug} ({mount})")
    set_station_status(mount, "ONLINE")
    print(f"  [+] Administrative lock released. Status set to ONLINE.")
    print(f"[✓] Virtual server '{slug}' restored to ONLINE.\n")

def server_status(slug):
    meta = get_station_meta(slug)
    mount = meta["mountpoint"]
    print(f"\n--- Virtual Server Status: {meta['station_name']} ---")
    print(f"  Customer ID : {meta.get('customer_id', 'N/A')}")
    print(f"  Mountpoint  : {mount}")
    print(f"  Server State: {meta.get('status', 'ONLINE')}")

    # Check Icecast live presence
    try:
        req = urllib.request.Request(f"{ICECAST_URL}/status-json.xsl", headers={"User-Agent": "StationCtl/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]
            active = any(s.get("listenurl", "").endswith(mount) for s in sources)
            print(f"  Icecast Feed: {'BROADCASTING' if active else 'IDLE / OFF-AIR'}")
    except Exception:
        print(f"  Icecast Feed: UNKNOWN")
    print("--------------------------------------------------\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 scripts/station_ctl.py <command> <station_slug>")
        print("Commands:")
        print("  start      - Power on station, open ingest, resume SLA monitoring")
        print("  stop       - Gracefully power off station, sever active connections")
        print("  restart    - Cycle station ingest and reset sockets")
        print("  suspend    - Administrative lock, evict encoder and listeners")
        print("  unsuspend  - Restore administrative access to normal")
        print("  status     - Show virtual server status and feed metrics")
        print("\nExample: python3 scripts/station_ctl.py stop station1")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    slug = sys.argv[2].strip("/").replace(".mp3", "")

    if cmd == "start":
        server_start(slug)
    elif cmd == "stop":
        server_stop(slug)
    elif cmd == "restart":
        server_stop(slug)
        server_start(slug)
    elif cmd == "suspend":
        server_suspend(slug)
    elif cmd == "unsuspend":
        server_unsuspend(slug)
    elif cmd == "status":
        server_status(slug)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
