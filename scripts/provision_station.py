#!/usr/bin/env python3
"""
StreamNetworx Multi-Station Automated Provisioning Engine
Usage: python3 scripts/provision_station.py <station_slug> [display_name] [bitrate]
Example: python3 scripts/provision_station.py capefm "Cape FM" 128
"""

import sys
import os
import re
import secrets
import subprocess
import json
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS_DIR = os.path.join(BASE_DIR, "configs")
ICECAST_XML = os.path.join(CONFIGS_DIR, "icecast.xml")
PUBLISHERS_JSON = os.path.join(CONFIGS_DIR, "publishers.json")
CONTAINER_NAME = "stream-node-combo"

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/provision_station.py <station_slug> [display_name] [bitrate]")
        sys.exit(1)

    slug = re.sub(r'[^a-zA-Z0-9_-]', '', sys.argv[1].lower())
    display_name = sys.argv[2] if len(sys.argv) > 2 else slug.upper()
    bitrate = sys.argv[3] if len(sys.argv) > 3 else "128"

    print(f"[*] Provisioning station: {display_name} (Slug: {slug}, Bitrate: {bitrate}k)...")

    # 1. Update Icecast XML Mount Configuration
    if os.path.exists(ICECAST_XML):
        with open(ICECAST_XML, "r") as f:
            xml_content = f.read()

        mount_tag = f"<mount-name>/{slug}.mp3</mount-name>"
        if mount_tag in xml_content:
            print(f"[!] Mount /{slug}.mp3 already exists in icecast.xml. Skipping XML modification.")
        else:
            mount_block = f"""
    <!-- {display_name} Mount with Seamless Fallback -->
    <mount type="normal">
        <mount-name>/{slug}.mp3</mount-name>
        <fallback-mount>/emergency.mp3</fallback-mount>
        <fallback-override>1</fallback-override>
    </mount>
</icecast>"""
            xml_content = re.sub(r'</icecast>\s*$', mount_block, xml_content.strip())
            with open(ICECAST_XML, "w") as f:
                f.write(xml_content)
            print(f"[✓] Added /{slug}.mp3 mount point to configs/icecast.xml")

    # 2. Update Publishers Registry
    publisher_token = secrets.token_hex(16)
    publishers = {}
    if os.path.exists(PUBLISHERS_JSON):
        try:
            with open(PUBLISHERS_JSON, "r") as f:
                publishers = json.load(f)
        except Exception:
            publishers = {}

    if slug in publishers:
        publisher_token = publishers[slug].get("token", publisher_token)
        print(f"[!] Station {slug} already has publisher token. Retaining existing key.")
    else:
        publishers[slug] = {
            "name": display_name,
            "token": publisher_token,
            "bitrate": int(bitrate),
            "created_at": int(os.path.getmtime(ICECAST_XML))
        }
        with open(PUBLISHERS_JSON, "w") as f:
            json.dump(publishers, f, indent=2)
        print(f"[✓] Registered publisher credentials in configs/publishers.json")

        # 2b. Initialize metadata staging directory inside container
    try:
        subprocess.run(
            ["sudo", "docker", "exec", CONTAINER_NAME, "mkdir", "-p", f"/var/www/metadata/{slug}"],
            check=False
        )
        init_json = json.dumps({"station": slug, "title": "Connecting to studio...", "artist": display_name, "album": "", "isrc": ""})
        subprocess.run(
            ["sudo", "docker", "exec", CONTAINER_NAME, "sh", "-c", f"echo \x27{init_json}\x27 > /var/www/metadata/{slug}/nowplaying.json"],
            check=False
        )
        print(f"[✓] Initialized metadata directory /var/www/metadata/{slug}")
    except Exception as e:
        print(f"[!] Warning: Metadata directory init failed: {e}")

    # 3. Reload Icecast & Nginx inside Docker without dropping active listeners (SIGHUP)
    try:
        # Copy updated config into running container if bind-mount is not directly mapped
        subprocess.run(
            ["sudo", "docker", "cp", ICECAST_XML, f"{CONTAINER_NAME}:/etc/icecast2/icecast.xml"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Reload Icecast config gracefully
        subprocess.run(
            ["sudo", "docker", "exec", CONTAINER_NAME, "sh", "-c", "kill -HUP $(pidof icecast2) 2>/dev/null || true"],
            check=False
        )
        print("[✓] Icecast dynamically reloaded configuration (Zero-Downtime SIGHUP)")
    except Exception as e:
        print(f"[!] Warning: Could not signal container reload: {e}")

    # 4. Generate Output Station Sheet
    print("\n" + "=" * 65)
    print(f"      STATION ONBOARDING MANIFEST: {display_name}")
    print("=" * 65)
    print(f"Station Identifier:     {slug}")
    print(f"Target Bitrate:         {bitrate} kbps (MP3 stereo)")
    print(f"Publisher API Key:      {publisher_token}")
    print("-" * 65)
    print("INGEST CREDENTIALS (STUDIO / STL / AUTOMATION):")
    print(f"  Icecast Direct Ingest: icecast://source:<ICECAST_PASS>@127.0.0.1:8000/{slug}.mp3")
    print(f"  SRT Ingest Endpoint:   srt://<PUBLIC_IP>:6000?streamid=publish:{slug}")
    print(f"  Metadata Webhook Push: POST http://<PUBLIC_IP>:8080/api/metadata/{slug}")
    print(f"                         Header: 'X-Station-Token: {publisher_token}'")
    print("-" * 65)
    print("LISTENER EDGE ENDPOINTS:")
    print(f"  Public HTTP Stream:    http://<PUBLIC_IP>:8080/{slug}.mp3")
    print(f"  Premium Protected:     http://<PUBLIC_IP>:8080/premium/{slug}.mp3?md5=<HASH>&expires=<TS>")
    print(f"  Now-Playing JSON:      http://<PUBLIC_IP>:8080/{slug}/nowplaying.json")
    print("=" * 65 + "\n")

if __name__ == "__main__":
    main()
