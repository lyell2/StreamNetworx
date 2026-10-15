#!/usr/bin/env python3
import os, sys, json, time, base64, socket, threading
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque

PORT = int(os.getenv("METADATA_PORT", "5000"))
ICECAST_URL = os.getenv("ICECAST_URL", "http://127.0.0.1:8000")
ICECAST_USER = os.getenv("ICECAST_ADMIN_USER", "admin")
ICECAST_PASS = os.getenv("ICECAST_ADMIN_PASS", "HackmeAdminPass")
WEB_DIR = os.getenv("METADATA_WEB_DIR", "/var/www/metadata")
CH_URL = os.getenv("CLICKHOUSE_URL", "http://clickhouse-hub:8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.getenv("CLICKHOUSE_PASSWORD", "ClickHousePass123")
AUTH_FILE = os.getenv("AUTH_TOKENS_FILE", "/etc/streaming/publishers.json")
DEFAULT_KEY = os.getenv("DEFAULT_PUBLISH_KEY", "secret_token_123")
MGMT_KEY = os.getenv("MANAGEMENT_API_KEY", "")

stream_histories = {}
stream_current = {}
stream_versions = {}
LOCK = threading.Lock()
os.makedirs(WEB_DIR, exist_ok=True)

def is_authorized(path, user, pw, ip, action):
    if action in ("read", "api", "playback") or (ip in ("127.0.0.1", "::1") and action != "publish"):
        return True
    station = path.strip("/").split("/")[-1]
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE) as tf:
                toks = json.load(tf)
                exp = toks.get(station)
                if exp and (pw == exp or user == exp):
                    return True
        except Exception:
            pass
    return pw == DEFAULT_KEY or user == DEFAULT_KEY

def log_playout(station, itype, artist, title, dur, started):
    sql = (
        f"INSERT INTO radio_analytics.playout_log (station_id, item_type, artist, title, duration_ms, started_at, ended_at) VALUES "
        f"('{station.replace(chr(39), chr(92)+chr(39))}', '{itype.lower()}', "
        f"'{artist.replace(chr(39), chr(92)+chr(39))}', '{title.replace(chr(39), chr(92)+chr(39))}', "
        f"{dur}, {started}, {started + (dur if dur > 0 else 180000)})"
    )
    def _run():
        try:
            req = urllib.request.Request(f"{CH_URL}/?user={CH_USER}&password={CH_PASS}", data=sql.encode("utf-8"))
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

def get_concurrency():
    sql = "SELECT mount, sum(total_sessions) s, sum(qualified_sessions) q, sum(total_bytes) b FROM radio_analytics.concurrency_1m_rollup WHERE minute >= now() - INTERVAL 2 MINUTE GROUP BY mount FORMAT JSON"
    try:
        url = f"{CH_URL}/?user={CH_USER}&password={CH_PASS}&query={urllib.parse.quote(sql)}"
        with urllib.request.urlopen(url, timeout=2) as r:
            rows = json.loads(r.read().decode()).get("data", [])
            out = {x["mount"]: {"total": int(x["s"]), "qualified": int(x["q"]), "bytes": int(x["b"])} for x in rows}
            return {"status": "ok", "timestamp": int(time.time()), "concurrency": out}
    except Exception as e:
        return {"status": "error", "message": str(e), "concurrency": {}}

def update_icy(mount, text):
    try:
        url = f"{ICECAST_URL}/admin/metadata?" + urllib.parse.urlencode({"mode": "updinfo", "mount": mount, "song": text})
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, ICECAST_USER, ICECAST_PASS)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
        opener.open(urllib.request.Request(url), timeout=2)
    except Exception:
        pass

def process_track(mount, artist, title, itype="Song", dur=0):
    mount = ("/" + mount.strip("/")).replace(".mp3", "") + ".mp3"
    station = mount.strip("/").replace(".mp3", "")
    now_ms = int(time.time() * 1000)
    if itype.lower() not in ("song", "music", "track"):
        return

    with LOCK:
        stream_versions[mount] = stream_versions.get(mount, 1000) + 1
        ver = stream_versions[mount]
        if mount not in stream_histories:
            stream_histories[mount] = deque(maxlen=10)
        if mount in stream_current:
            stream_histories[mount].appendleft(stream_current[mount])

        track = {"artist": artist, "title": title, "item_type": itype, "started_at": now_ms, "duration_ms": dur*1000 if dur else 180000}
        stream_current[mount] = track
        payload = {"station": station, "version": ver, "mountpoint": mount, "synced_at": now_ms, "now_playing": track, "history": list(stream_histories[mount])}

    display = f"{artist} - {title}" if artist else title
    update_icy(mount, display)

    tdir = os.path.join(WEB_DIR, station)
    os.makedirs(tdir, exist_ok=True)
    tmp, target = os.path.join(tdir, "nowplaying.json.tmp"), os.path.join(tdir, "nowplaying.json")
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, target)
    except Exception:
        pass

    log_playout(station, itype, artist, title, track["duration_ms"], now_ms)
    print(f"[v] Track updated: {mount} (v{ver}): {display}", flush=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): return

    def _check_mgmt_auth(self, qs):
        if not MGMT_KEY:
            return True
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer ") and auth_hdr.split(" ", 1)[1].strip() == MGMT_KEY:
            return True
        if qs.get("key", [""])[0] == MGMT_KEY or qs.get("api_key", [""])[0] == MGMT_KEY:
            return True
        return False

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)

        # 1. Secured Concurrency API
        if p.path == "/api/concurrency":
            if not self._check_mgmt_auth(q):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"error","message":"unauthorized: invalid or missing management token"}')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(get_concurrency()).encode())
            return

        # 2. Playout GET Shim (Myriad OCP / RadioDJ / Query-String Pushes)
        # Supports: /api/metadata/push?station=...&artist=...&title=...
        # Also supports legacy Icecast updinfo parameter conventions (&song=Artist - Title)
        mount = q.get("mount", [f"/{q.get('station', ['station1'])[0]}.mp3"])[0]
        artist = q.get("artist", [""])[0]
        title = q.get("title", [""])[0]
        dur = int(q.get("duration", [0])[0])
        itype = q.get("type", ["Song"])[0]

        # Handle compound song param (e.g. "?song=Artist - Title")
        if not artist and not title and "song" in q:
            raw_song = q["song"][0]
            if " - " in raw_song:
                artist, title = raw_song.split(" - ", 1)
            else:
                title = raw_song

        if title or artist:
            process_track(mount, artist, title, itype, dur)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","message":"metadata updated"}')
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing fields (require artist/title or song)")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        l = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(l).decode("utf-8", errors="ignore") if l else ""

        # MediaMTX Authentication Webhook
        if p.path == "/api/v1/auth":
            try:
                d = json.loads(body)
                if is_authorized(d.get("path",""), d.get("user",""), d.get("password",""), d.get("ip",""), d.get("action","")):
                    self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
                else:
                    self.send_response(401); self.end_headers(); self.wfile.write(b"unauthorized")
            except Exception:
                self.send_response(400); self.end_headers()
            return

        # Playout Metadata Ingestion (JSON or XML)
        if p.path in ("/api/metadata/push", "/api/metadata", "/"):
            artist, title, mount, dur, itype = "", "", "/station1.mp3", 0, "Song"

            # Check if payload is XML (RCS Zetta / PlayoutONE / OCP XML)
            if body.strip().startswith("<"):
                try:
                    root = ET.fromstring(body)
                    artist = root.findtext(".//Artist") or root.findtext(".//artist") or ""
                    title = root.findtext(".//Title") or root.findtext(".//title") or ""
                    station = root.findtext(".//Station") or root.findtext(".//station") or "station1"
                    mount = f"/{station}.mp3"
                    raw_dur = root.findtext(".//Duration") or root.findtext(".//duration") or "0"
                    dur = int(float(raw_dur))
                    itype = root.findtext(".//ItemType") or root.findtext(".//item_type") or "Song"
                except Exception as e:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(f"XML parse error: {str(e)}".encode())
                    return
            else:
                # Default JSON Parsing
                try:
                    d = json.loads(body)
                    artist = d.get("artist", "")
                    title = d.get("title", "")
                    mount = d.get("mountpoint") or f"/{d.get('station', 'station1')}.mp3"
                    dur = int(d.get("duration", 0))
                    itype = d.get("type", "Song")
                except Exception as e:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(f"JSON parse error: {str(e)}".encode())
                    return

            if title or artist:
                process_track(mount, artist, title, itype, dur)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok","message":"metadata ingested"}')
            else:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"missing track fields")
            return

        self.send_response(404); self.end_headers()

if __name__ == "__main__":
    print(f"=== StreamNetworx Metadata Service Active on :{PORT} ===", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
