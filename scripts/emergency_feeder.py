#!/usr/bin/env python3
import os
import sys
import time
import subprocess

AUDIO_FILE = os.getenv("EMERGENCY_CART_PATH", "/etc/streaming/emergency_loop.mp3")
ICECAST_HOST = os.getenv("ICECAST_HOST", "127.0.0.1")
ICECAST_PORT = os.getenv("ICECAST_PORT", "8000")
ICECAST_PASS = os.getenv("ICECAST_SOURCE_PASS", "HackmeIcecastPass")
MOUNTPOINT = os.getenv("EMERGENCY_MOUNT", "/emergency.mp3")

ICECAST_STREAM_URL = f"icecast://source:{ICECAST_PASS}@{ICECAST_HOST}:{ICECAST_PORT}{MOUNTPOINT}"

def run_feeder():
    if not os.path.exists(AUDIO_FILE):
        print(f"[!] Emergency cart not found at {AUDIO_FILE}. Feeder sleeping...", flush=True)
        time.sleep(10)
        return

    print(f"[*] Starting Persistent Emergency Feeder for {MOUNTPOINT}...", flush=True)

    cmd = [
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", AUDIO_FILE,
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-content_type", "audio/mpeg",
        "-ice_name", "Emergency Backup Cart",
        "-ice_description", "Automated Fallback Audio Loop",
        "-ice_public", "0",
        "-f", "mp3",
        ICECAST_STREAM_URL
    ]

    while True:
        try:
            print(f"[✓] Launching FFmpeg feeder stream to {MOUNTPOINT}...", flush=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, stderr = proc.communicate()
            if proc.returncode != 0:
                print(f"[!] FFmpeg feeder exited with code {proc.returncode}. Reconnecting in 3s...", flush=True)
                time.sleep(3)
        except Exception as e:
            print(f"[!] Emergency Feeder exception: {e}. Retrying in 5s...", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    run_feeder()
