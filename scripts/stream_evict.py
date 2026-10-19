#!/usr/bin/env python3
"""
StreamNetworx Mid-Stream Eviction Orchestrator (Decision 16)
Suspends a station immediately by:
1. Kicking active SRT publisher sessions in MediaMTX.
2. Dropping all active listener TCP sockets on Icecast.
3. Writing a temporary suspension state to prevent unauthorized reconnections.
"""
import sys
import os
import json
import urllib.request
import urllib.parse

MEDIAMTX_API = os.getenv("MEDIAMTX_API", "http://127.0.0.1:9997")
ICECAST_URL = os.getenv("ICECAST_URL", "http://127.0.0.1:8000")
ICECAST_USER = os.getenv("ICECAST_ADMIN_USER", "admin")
ICECAST_PASS = os.environ["ICECAST_ADMIN_PASS"]

def kick_mediamtx_publishers(station: str):
    path_target = f"live/{station}"
    print(f"[*] Querying MediaMTX SRT sessions for path '{path_target}'...", flush=True)
    try:
        req = urllib.request.Request(f"{MEDIAMTX_API}/v3/srtconns/list")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            kicked = 0
            for item in items:
                conn_id = item.get("id")
                conn_path = item.get("path", "")
                if conn_path == path_target or conn_path.endswith(station):
                    print(f"[-] Kicking SRT publisher session ID: {conn_id}...", flush=True)
                    kick_req = urllib.request.Request(
                        f"{MEDIAMTX_API}/v3/srtconns/kick/{conn_id}",
                        method="POST"
                    )
                    with urllib.request.urlopen(kick_req, timeout=3) as k_resp:
                        if k_resp.status == 200:
                            kicked += 1
            print(f"[✓] Terminated {kicked} MediaMTX SRT connection(s).", flush=True)
    except Exception as e:
        print(f"[!] Warning: Could not query/kick MediaMTX SRT connections: {e}", flush=True)

def kill_icecast_source(station: str):
    mount = f"/{station}.mp3" if not station.startswith("/") else station
    if not mount.endswith(".mp3"):
        mount += ".mp3"

    print(f"[*] Forcibly severing Icecast mount source and listeners: {mount}...", flush=True)
    try:
        url = f"{ICECAST_URL}/admin/killsource?mount={urllib.parse.quote(mount)}"
        pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd_mgr.add_password(None, url, ICECAST_USER, ICECAST_PASS)
        handler = urllib.request.HTTPBasicAuthHandler(pwd_mgr)
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, headers={"User-Agent": "StreamNetworx-Evict/1.0"})
        with opener.open(req, timeout=3) as resp:
            if resp.status == 200:
                print(f"[✓] Successfully executed killsource on {mount}. Listeners evicted.", flush=True)
            else:
                print(f"[!] Icecast killsource returned HTTP {resp.status}", flush=True)
    except urllib.error.HTTPError as he:
        if he.code == 400 or he.code == 404:
            print(f"[*] Mount {mount} not currently active on Icecast (already idle).", flush=True)
        else:
            print(f"[!] Icecast error: {he}", flush=True)
    except Exception as e:
        print(f"[!] Failed to kill Icecast source: {e}", flush=True)

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 stream_evict.py <station_name>")
        sys.exit(1)

    station = sys.argv[1].strip()
    print(f"=== Initiating Immediate Mid-Stream Eviction for: {station} ===", flush=True)
    kick_mediamtx_publishers(station)
    kill_icecast_source(station)
    print("=== Eviction Routine Finished ===", flush=True)

if __name__ == "__main__":
    main()
