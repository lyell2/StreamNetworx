#!/usr/bin/env python3
import os, sys, json, time, base64, socket, threading, asyncio, re, hmac
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque

PORT = int(os.getenv("METADATA_PORT", "5000"))
ICECAST_URL = os.getenv("ICECAST_URL", "http://127.0.0.1:8000")
ICECAST_USER = os.getenv("ICECAST_ADMIN_USER", "admin")
ICECAST_PASS = os.environ["ICECAST_ADMIN_PASS"]
WEB_DIR = os.getenv("METADATA_WEB_DIR", "/var/www/metadata")
CH_URL = os.getenv("CLICKHOUSE_URL", "http://clickhouse-hub:8123")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS = os.environ["CLICKHOUSE_PASSWORD"]
AUTH_FILE = os.getenv("AUTH_TOKENS_FILE", "/etc/streaming/publishers.json")
MGMT_KEY = os.getenv("MANAGEMENT_API_KEY", "")
NODE_ID = os.getenv("NODE_ID", "sa-1-monitoring")
NATS_URL = os.getenv("NATS_URL", "nats://central-nats:4222")

stream_histories = {}
stream_current = {}
stream_versions = {}
stream_synced_at = {}
LOCK = threading.Lock()
metadata_bus = None
os.makedirs(WEB_DIR, exist_ok=True)

def is_authorized(path, user, pw, ip, action):
    if action in ("read", "api", "playback") or (ip in ("127.0.0.1", "::1") and action != "publish"):
        return True
    station = path.strip("/").split("/")[-1]
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE) as tf:
                toks = json.load(tf)
                entry = toks.get(station)
                expected = entry.get("token") if isinstance(entry, dict) else entry
                if expected and any(hmac.compare_digest(value, expected) for value in (pw, user) if value):
                    return True
        except Exception:
            pass
    return False

def station_token(station):
    try:
        with open(AUTH_FILE) as token_file:
            registry = json.load(token_file)
        entry = registry.get(station)
        return entry.get("token") if isinstance(entry, dict) else entry
    except Exception:
        return None

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
    sql = "SELECT mount, sum(total_sessions) s, sum(qualified_sessions) q, sum(total_bytes) b FROM radio_analytics.concurrency_1m_active_rollup WHERE minute >= now() - INTERVAL 2 MINUTE GROUP BY mount FORMAT JSON"
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
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", station):
        raise ValueError("Invalid station identifier")
    now_ms = int(time.time() * 1000)
    if itype.lower() not in ("song", "music", "track"):
        return

    with LOCK:
        stream_versions[mount] = stream_versions.get(mount, 1000) + 1
        ver = stream_versions[mount]
        stream_synced_at[mount] = now_ms
        if mount not in stream_histories:
            stream_histories[mount] = deque(maxlen=10)
        if mount in stream_current:
            stream_histories[mount].appendleft(stream_current[mount])

        track = {"artist": artist, "title": title, "item_type": itype, "started_at": now_ms, "duration_ms": dur*1000 if dur else 180000}
        stream_current[mount] = track
        payload = {"station": station, "version": ver, "mountpoint": mount, "synced_at": now_ms, "origin_node": NODE_ID, "now_playing": track, "history": list(stream_histories[mount])}

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
    if metadata_bus:
        metadata_bus.publish(payload)
    print(f"[v] Track updated: {mount} (v{ver}): {display}", flush=True)

def apply_remote_metadata(payload):
    mount = str(payload.get("mountpoint", ""))
    station = str(payload.get("station", ""))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", station):
        return
    if mount != f"/{station}.mp3":
        return

    try:
        version = int(payload["version"])
        synced_at = int(payload["synced_at"])
        track = payload["now_playing"]
    except (KeyError, TypeError, ValueError):
        return

    with LOCK:
        if synced_at <= stream_synced_at.get(mount, 0):
            return
        stream_synced_at[mount] = synced_at
        stream_versions[mount] = version
        stream_histories[mount] = deque(payload.get("history", []), maxlen=10)
        stream_current[mount] = track

    display = f"{track.get('artist', '')} - {track.get('title', '')}".strip(" -")
    update_icy(mount, display)
    tdir = os.path.join(WEB_DIR, station)
    os.makedirs(tdir, exist_ok=True)
    target = os.path.join(tdir, "nowplaying.json")
    tmp = target + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, target)
    except Exception as exc:
        print(f"[!] Remote metadata write failed for {station}: {exc}", flush=True)

class MetadataBus:
    def __init__(self):
        self.loop = None
        self.client = None

    async def _receive(self, message):
        try:
            payload = json.loads(message.data.decode("utf-8"))
            if payload.get("origin_node") != NODE_ID:
                apply_remote_metadata(payload)
        except Exception as exc:
            print(f"[!] NATS metadata event rejected: {exc}", flush=True)

    async def _connect(self):
        import nats

        while True:
            try:
                self.client = await nats.connect(
                    NATS_URL,
                    name=f"streamnetworx-{NODE_ID}",
                    reconnect_time_wait=2,
                    max_reconnect_attempts=-1
                )
                await self.client.subscribe("metadata.*", cb=self._receive)
                safe_endpoint = urllib.parse.urlsplit(NATS_URL)
                print(f"[NATS] Metadata bus connected at {safe_endpoint.hostname}:{safe_endpoint.port or 4222}", flush=True)
                return
            except Exception as exc:
                print(f"[NATS] Metadata bus unavailable: {exc}", flush=True)
                await asyncio.sleep(5)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())
        self.loop.run_forever()

    def start(self):
        threading.Thread(target=self._run, name="metadata-nats", daemon=True).start()

    def publish(self, payload):
        if self.client and self.client.is_connected:
            subject = f"metadata.{payload['station']}"
            asyncio.run_coroutine_threadsafe(
                self.client.publish(subject, json.dumps(payload).encode("utf-8")),
                self.loop
            )

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): return

    def _check_mgmt_auth(self, qs):
        if not MGMT_KEY:
            return False
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer ") and auth_hdr.split(" ", 1)[1].strip() == MGMT_KEY:
            return True
        return False

    def _authorize_station(self, station):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", station):
            return False
        expected = station_token(station)
        provided = self.headers.get("X-Station-Token", "")
        authorization = self.headers.get("Authorization", "")
        if not provided and authorization.startswith("Bearer "):
            provided = authorization.split(" ", 1)[1].strip()
        return bool(expected and provided and hmac.compare_digest(expected, provided))

    def _send_json_error(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "error", "message": message}).encode())

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

        # 2. Authenticated playout GET shim (Myriad OCP / RadioDJ)
        # Supports: /api/metadata/push?station=...&artist=...&title=...
        # Also supports legacy Icecast updinfo parameter conventions (&song=Artist - Title)
        route_match = re.fullmatch(r"/api/metadata/([a-zA-Z0-9_-]{2,64})", p.path)
        if p.path not in ("/api/metadata", "/api/metadata/push") and not route_match:
            self.send_error(404)
            return
        station = route_match.group(1) if route_match else q.get("station", [""])[0]
        if not self._authorize_station(station):
            self._send_json_error(401, "unauthorized station token")
            return
        mount = f"/{station}.mp3"
        requested_mount = q.get("mount", [mount])[0]
        if requested_mount != mount:
            self._send_json_error(403, "station token does not authorize this mount")
            return
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
            try:
                process_track(mount, artist, title, itype, dur)
            except ValueError:
                self._send_json_error(400, "invalid station")
                return
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
        if p.path in ("/api/metadata/push", "/api/metadata") or re.fullmatch(r"/api/metadata/[a-zA-Z0-9_-]{2,64}", p.path):
            artist, title, mount, dur, itype = "", "", "/station1.mp3", 0, "Song"
            station = ""
            route_match = re.fullmatch(r"/api/metadata/([a-zA-Z0-9_-]{2,64})", p.path)

            # Check if payload is XML (RCS Zetta / PlayoutONE / OCP XML)
            if body.strip().startswith("<"):
                try:
                    root = ET.fromstring(body)
                    artist = root.findtext(".//Artist") or root.findtext(".//artist") or ""
                    title = root.findtext(".//Title") or root.findtext(".//title") or ""
                    station = route_match.group(1) if route_match else (root.findtext(".//Station") or root.findtext(".//station") or "")
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
                    station = route_match.group(1) if route_match else d.get("station", "")
                    mount = d.get("mountpoint") or f"/{station}.mp3"
                    dur = int(d.get("duration", 0))
                    itype = d.get("type", "Song")
                except Exception as e:
                    self.send_response(400); self.end_headers()
                    self.wfile.write(f"JSON parse error: {str(e)}".encode())
                    return

            route_station = route_match.group(1) if route_match else ""
            if route_station and station and route_station != station:
                self._send_json_error(403, "station path and payload do not match")
                return
            station = route_station or station
            if not self._authorize_station(station):
                self._send_json_error(401, "unauthorized station token")
                return
            if mount != f"/{station}.mp3":
                self._send_json_error(403, "station token does not authorize this mount")
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
    metadata_bus = MetadataBus()
    metadata_bus.start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
