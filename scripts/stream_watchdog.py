import os
import subprocess
import time
import re
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor

CH_URL = os.getenv("CLICKHOUSE_URL", "http://host.docker.internal:8123/")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "ClickHousePass123")
ICECAST_STATUS_URL = os.getenv("ICECAST_STATUS_URL", "http://host.docker.internal:8000/status-json.xsl")
ICECAST_STREAM_BASE = os.getenv("ICECAST_STREAM_BASE", "http://host.docker.internal:8000")
NODE_ID = os.getenv("NODE_ID", "sa-1-monitoring")
SILENCE_THRESHOLD_DB = float(os.getenv("SILENCE_THRESHOLD_DB", "-50.0"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")

consecutive_silence = {}

def get_stream_statuses():
    statuses = {}
    try:
        pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd_mgr.add_password(None, CH_URL, CH_USER, CH_PASS)
        handler = urllib.request.HTTPBasicAuthHandler(pwd_mgr)
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(CH_URL, data=b"SELECT mountpoint, status FROM streamnet.streams FORMAT TabSeparated")
        with opener.open(req, timeout=3) as resp:
            for line in resp.read().decode().strip().split("\n"):
                if line:
                    parts = line.split("\t")
                    if len(parts) == 2:
                        statuses[parts[0]] = parts[1]
    except Exception:
        pass
    return statuses

def send_alert(path, state, count):
    if not WEBHOOK_URL:
        return
    msg = {"text": f"🚨 [StreamNetworx Alert] Stream {path} on {NODE_ID} entered {state}! Duration: {count * POLL_INTERVAL}s"}
    try:
        req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(msg).encode("utf-8"), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        print(f"Failed to send alert: {e}")

def get_all_active_mounts():
    mounts = []
    try:
        req = urllib.request.Request(ICECAST_STATUS_URL, headers={"User-Agent": "StreamNetworxWatchdog/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            sources = data.get("icestats", {}).get("source", [])
            if isinstance(sources, dict):
                sources = [sources]
            for src in sources:
                url = src.get("listenurl", "")
                path = "/" + url.split("/", 3)[-1] if "/" in url else url
                mounts.append({
                    "path": path,
                    "listeners": int(src.get("listeners", 0)),
                    "bitrate": int(src.get("bitrate", 128))
                })
    except Exception:
        pass
    if not mounts:
        mounts.append({"path": "/station1.mp3", "listeners": 0, "bitrate": 128})
    return mounts

def probe_mount(mount, stream_statuses):
    path = mount["path"]
    status = stream_statuses.get(path, "ONLINE")

    if status in ("STOPPED", "SUSPENDED"):
        return path, status, -100.0, -100.0, 0, mount.get("bitrate", 128)

    stream_url = f"{ICECAST_STREAM_BASE}{path}"
    cmd = [
        "ffmpeg", "-nostdin", "-t", "3",
        "-i", stream_url,
        "-af", "astats=metadata=1:reset=1",
        "-f", "null", "-"
    ]
    try:
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, timeout=6)
        output = res.stderr
        
        rms_match = re.search(r"RMS level dB:\s+([-\d\.]+)", output)
        peak_match = re.search(r"Peak level dB:\s+([-\d\.]+)", output)
        
        rms = float(rms_match.group(1)) if rms_match else -100.0
        peak = float(peak_match.group(1)) if peak_match else -100.0
        
        state = "SILENCE" if rms <= SILENCE_THRESHOLD_DB else "HEALTHY"
        return path, state, rms, peak, mount["listeners"], mount["bitrate"]
    except Exception:
        return path, "OFFLINE", -100.0, -100.0, mount["listeners"], mount["bitrate"]

def push_metrics_batch(results):
    if not results:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    values = []
    for path, state, rms, peak, listeners, bitrate in results:
        values.append(f"('{ts}', '{NODE_ID}', '{path}', 0, {listeners}, '{state}', {rms:.2f}, {peak:.2f}, {bitrate})")
        
        if state in ("SILENCE", "OFFLINE"):
            consecutive_silence[path] = consecutive_silence.get(path, 0) + 1
            if consecutive_silence[path] == 3:
                send_alert(path, state, consecutive_silence[path])
        else:
            consecutive_silence[path] = 0

    query = f"INSERT INTO streamnet.stream_metrics (timestamp, node_id, path, bytes_received, readers_count, state, rms_db, peak_db, bitrate_kbps) VALUES " + ", ".join(values)
    try:
        pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwd_mgr.add_password(None, CH_URL, CH_USER, CH_PASS)
        handler = urllib.request.HTTPBasicAuthHandler(pwd_mgr)
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(CH_URL, data=query.encode("utf-8"))
        opener.open(req, timeout=4)
    except Exception as e:
        print(f"Failed to push metrics: {e}")

if __name__ == "__main__":
    print(f"Starting Multi-Stream Dynamic Watchdog on node {NODE_ID}...")
    while True:
        stream_statuses = get_stream_statuses()
        mounts = get_all_active_mounts()
        
        known_mounts = {m["path"] for m in mounts}
        for path, status in stream_statuses.items():
            if status in ("STOPPED", "SUSPENDED") and path not in known_mounts:
                mounts.append({"path": path, "listeners": 0, "bitrate": 128})

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(lambda m: probe_mount(m, stream_statuses), mounts))
        push_metrics_batch(results)
        for path, state, rms, peak, listeners, _ in results:
            print(f"[{time.strftime('%H:%M:%S')}] {path:<16} State: {state:<9} | RMS: {rms:6.1f} dB | Listeners: {listeners}")
        time.sleep(POLL_INTERVAL)
