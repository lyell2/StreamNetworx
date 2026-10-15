#!/usr/bin/env python3
import subprocess
import re
import sys
import time
import os
import json
import urllib.request
import urllib.parse

STREAM_URL = os.getenv("WATCHDOG_STREAM_URL", "http://127.0.0.1:8000/station1.mp3")
SILENCE_THRESH = os.getenv("SILENCE_THRESHOLD_DB", "-50dB")
SILENCE_DURATION = os.getenv("SILENCE_DURATION_SEC", "5")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
STATION_NAME = os.getenv("STATION_NAME", "Station 1")

re_start = re.compile(r"silence_start:\s*([0-9\.]+)")
re_end = re.compile(r"silence_end:\s*([0-9\.]+)\s*\|\s*silence_duration:\s*([0-9\.]+)")

last_alert_time = 0
ALERT_COOLDOWN = 60  # seconds between repeated alert pings

def dispatch_webhook(event_type, message, details=None):
    if not ALERT_WEBHOOK_URL:
        return
    payload = {
        "event": event_type,
        "station": STATION_NAME,
        "stream_url": STREAM_URL,
        "message": message,
        "timestamp": int(time.time()),
        "details": details or {}
    }
    # Formats cleanly for Discord, Slack, or generic HTTP collectors
    headers = {"Content-Type": "application/json"}
    body = {"content": f"🚨 **[{STATION_NAME}]** {message}"} if "discord" in ALERT_WEBHOOK_URL or "slack" in ALERT_WEBHOOK_URL else payload
    try:
        req = urllib.request.Request(ALERT_WEBHOOK_URL, data=json.dumps(body).encode("utf-8"), headers=headers)
        urllib.request.urlopen(req, timeout=4)
    except Exception as e:
        print(f"[Watchdog] Failed to send webhook alert: {e}", file=sys.stderr)

def monitor():
    global last_alert_time
    cmd = [
        "ffmpeg", "-nostdin",
        "-i", STREAM_URL,
        "-af", f"silencedetect=noise={SILENCE_THRESH}:d={SILENCE_DURATION}",
        "-f", "null", "-"
    ]
    print(f"[Watchdog] Monitoring {STATION_NAME} stream on {STREAM_URL} (threshold: {SILENCE_THRESH}, trigger: {SILENCE_DURATION}s)...", flush=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in iter(proc.stderr.readline, ""):
        if not line:
            break
        
        m_start = re_start.search(line)
        if m_start:
            now = time.time()
            t_sec = m_start.group(1)
            msg = f"DEAD AIR DETECTED: Silence began at stream position t={t_sec}s."
            print(f"[ALERT] [DEAD AIR] {msg}", flush=True)
            if now - last_alert_time > ALERT_COOLDOWN:
                dispatch_webhook("SILENCE_START", msg, {"start_offset": t_sec})
                # Evict frozen encoder to force fallback
                subprocess.run([sys.executable, "scripts/stream_evict.py", STATION_NAME], capture_output=True)
                last_alert_time = now

        m_end = re_end.search(line)
        if m_end:
            t_end = m_end.group(1)
            dur = m_end.group(2)
            msg = f"AUDIO RECOVERED: Audio resumed at t={t_end}s after {dur}s of silence."
            print(f"[RECOVERY] {msg}", flush=True)
            dispatch_webhook("SILENCE_RESOLVED", msg, {"end_offset": t_end, "duration_sec": dur})

    proc.wait()

if __name__ == "__main__":
    while True:
        try:
            monitor()
        except Exception as e:
            print(f"[Watchdog] Loop interrupted: {e}", flush=True)
        time.sleep(3)
