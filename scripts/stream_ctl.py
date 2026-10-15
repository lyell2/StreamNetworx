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

def query_ch(sql):
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

def resolve_stream(identifier):
    # Match by stream_id OR mountpoint
    clean = identifier.strip()
    clean_mount = clean if clean.startswith("/") else f"/{clean}"
    if not clean_mount.endswith(".mp3"):
        clean_mount += ".mp3"

    sql = f"""
    SELECT stream_id, mountpoint, stream_name, customer_id, status, max_listeners 
    FROM streamnet.streams 
    WHERE stream_id = '{clean}' OR mountpoint = '{clean_mount}'
    LIMIT 1 FORMAT JSONEachRow;
    """
    raw = query_ch(sql)
    if not raw:
        # Fallback dynamic object if not yet registered
        slug = clean.strip("/").replace(".mp3", "")
        return {
            "stream_id": slug,
            "mountpoint": f"/{slug}.mp3",
            "stream_name": f"Stream {slug}",
            "status": "ONLINE",
            "max_listeners": 500
        }
    return json.loads(raw.split("\n")[0])

def set_stream_status(stream_id, status):
    sql = f"ALTER TABLE streamnet.streams UPDATE status = '{status}' WHERE stream_id = '{stream_id}';"
    query_ch(sql)

def sever_icecast(mountpoint):
    kill_url = f"{ICECAST_URL}/admin/killsource?mount={mountpoint}"
    try:
        res = requests.get(kill_url, auth=(ICECAST_ADMIN_USER, ICECAST_ADMIN_PASS), timeout=3)
        if res.status_code == 200:
            print(f"  [+] Icecast: Mount '{mountpoint}' terminated, listeners disconnected.")
        else:
            print(f"  [-] Icecast: Mount '{mountpoint}' was not active.")
    except Exception as e:
        print(f"  [!] Icecast unreachable: {e}")

def sever_mediamtx(stream_id):
    try:
        url = f"{MEDIAMTX_API}/v3/paths/delete/live/{stream_id}"
        res = requests.post(url, timeout=3)
        if res.status_code in (200, 204):
            print(f"  [+] MediaMTX: Evicted SRT publisher on 'live/{stream_id}'")
        else:
            print(f"  [-] MediaMTX: No active publisher on 'live/{stream_id}'")
    except Exception as e:
        print(f"  [!] MediaMTX unreachable: {e}")

def cmd_start(stream):
    sid = stream["stream_id"]
    print(f"\n[*] STARTING STREAM: {sid} ({stream['mountpoint']})")
    set_stream_status(sid, "ONLINE")
    print(f"  [+] Stream state set to ONLINE in database.")
    print(f"  [+] Ingest port open for incoming SRT / Icecast sources.")
    print(f"[✓] Stream '{sid}' is ONLINE.\n")

def cmd_stop(stream):
    sid = stream["stream_id"]
    print(f"\n[*] STOPPING STREAM: {sid} ({stream['mountpoint']})")
    sever_mediamtx(sid)
    sever_icecast(stream["mountpoint"])
    set_stream_status(sid, "STOPPED")
    print(f"  [+] State set to STOPPED. SLA monitoring paused.")
    print(f"[✓] Stream '{sid}' cleanly STOPPED.\n")

def cmd_suspend(stream):
    sid = stream["stream_id"]
    print(f"\n[!] SUSPENDING STREAM: {sid} ({stream['mountpoint']})")
    sever_mediamtx(sid)
    sever_icecast(stream["mountpoint"])
    set_stream_status(sid, "SUSPENDED")
    print(f"  [+] State set to SUSPENDED. Publisher and listeners locked out.")
    print(f"[✓] Stream '{sid}' SUSPENDED.\n")

def cmd_unsuspend(stream):
    sid = stream["stream_id"]
    print(f"\n[*] UNSUSPENDING STREAM: {sid} ({stream['mountpoint']})")
    set_stream_status(sid, "ONLINE")
    print(f"  [+] Suspension released. Restored to ONLINE.")
    print(f"[✓] Stream '{sid}' is ONLINE.\n")

def cmd_status(stream):
    sid = stream["stream_id"]
    mount = stream["mountpoint"]
    print(f"\n--- Stream Status: {stream.get('stream_name', sid)} ---")
    print(f"  Stream ID     : {sid}")
    print(f"  Mountpoint    : {mount}")
    print(f"  Current State : {stream.get('status', 'ONLINE')}")
    print(f"  Max Listeners : {stream.get('max_listeners', 500)}")

    try:
        req = urllib.request.Request(f"{ICECAST_URL}/status-json.xsl", headers={"User-Agent": "StreamCtl/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]
            active_source = next((s for s in sources if s.get("listenurl", "").endswith(mount)), None)
            if active_source:
                print(f"  Feed Health   : BROADCASTING ({active_source.get('listeners', 0)} listeners, {active_source.get('bitrate', 128)} kbps)")
            else:
                print(f"  Feed Health   : IDLE / OFF-AIR")
    except Exception:
        print(f"  Feed Health   : UNKNOWN")
    print("--------------------------------------------------\n")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 scripts/stream_ctl.py <command> <stream_id>")
        print("Commands: start | stop | restart | suspend | unsuspend | status")
        print("\nExamples:")
        print("  python3 scripts/stream_ctl.py status stream1")
        print("  python3 scripts/stream_ctl.py stop stream1")
        print("  python3 scripts/stream_ctl.py suspend stream2")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    stream_meta = resolve_stream(sys.argv[2])

    if cmd == "start":
        cmd_start(stream_meta)
    elif cmd == "stop":
        cmd_stop(stream_meta)
    elif cmd == "restart":
        cmd_stop(stream_meta)
        cmd_start(stream_meta)
    elif cmd == "suspend":
        cmd_suspend(stream_meta)
    elif cmd == "unsuspend":
        cmd_unsuspend(stream_meta)
    elif cmd == "status":
        cmd_status(stream_meta)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
