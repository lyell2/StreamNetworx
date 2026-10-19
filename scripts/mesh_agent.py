import os
import sys
import json
import time
import socket
import subprocess
import threading
import ipaddress
import hashlib
import hmac
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESH_PORT = int(os.getenv("MESH_GOSSIP_PORT", "9111"))
WG_IFACE = "wg0"
WG_PORT = int(os.getenv("WG_PORT", "51820"))
JOIN_PORT = int(os.getenv("MESH_JOIN_PORT", "9112"))
MESH_BOOTSTRAP_URL = os.getenv("MESH_BOOTSTRAP_URL", "").strip()
PUBLIC_WG_ENDPOINT = os.getenv("PUBLIC_WG_ENDPOINT", "").strip()
MESH_SECRET = os.getenv("MESH_INTERNAL_SECRET", "").strip()
if len(MESH_SECRET) < 32:
    raise RuntimeError("MESH_INTERNAL_SECRET must be set to at least 32 characters")
raw_bp = os.getenv("BOOTSTRAP_PEERS", "").strip()
BOOTSTRAP_PEERS = [p.strip() for p in raw_bp.split(",") if p.strip() and ":" in p]
SUBNET = ipaddress.ip_network("10.10.0.0/24")

# Local state
peer_table = {}  # pubkey -> {"mesh_ip": str, "endpoint": str, "last_seen": float}
local_info = {"pubkey": None, "mesh_ip": None, "public_endpoint": None}
peer_lock = threading.RLock()
join_nonce_lock = threading.Lock()
join_nonces = {}
PEER_STATE_FILE = "/etc/wireguard/peers.json"
NATS_ROUTES_FILE = "/etc/wireguard/nats_routes.txt"
DOCKER_BRIDGE_NETWORK = ipaddress.ip_network("172.16.0.0/12")

def observed_peer_endpoint(message, source_ip):
    source = ipaddress.ip_address(source_ip)
    if source in DOCKER_BRIDGE_NETWORK:
        return f"{source_ip}:{message.get('wg_port', WG_PORT)}"
    return message.get("public_endpoint") or f"{source_ip}:{message.get('wg_port', WG_PORT)}"

def encode_message(payload):
    message = dict(payload)
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    message["signature"] = hmac.new(MESH_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    return json.dumps(message, separators=(",", ":")).encode()

def decode_message(data):
    message = json.loads(data.decode("utf-8"))
    signature = message.pop("signature", None)
    if not isinstance(signature, str):
        raise ValueError("unsigned mesh message")
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(MESH_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid mesh message signature")
    return message

def public_endpoint():
    endpoint = PUBLIC_WG_ENDPOINT
    if not endpoint:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as response:
            endpoint = response.read().decode("ascii").strip()
    host = endpoint.rsplit(":", 1)[0] if endpoint.count(":") == 1 else endpoint
    ipaddress.ip_address(host)
    port = endpoint.rsplit(":", 1)[1] if endpoint.count(":") == 1 else str(WG_PORT)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid PUBLIC_WG_ENDPOINT port")
    return f"{host}:{port}"

def peer_snapshot(exclude_pubkey=None):
    with peer_lock:
        peers = [
            {"pubkey": key, **value}
            for key, value in peer_table.items()
            if key != exclude_pubkey
        ]
    if local_info["pubkey"] != exclude_pubkey and local_info["mesh_ip"]:
        peers.append({
            "pubkey": local_info["pubkey"],
            "mesh_ip": local_info["mesh_ip"],
            "endpoint": local_info["public_endpoint"],
            "wg_port": WG_PORT,
            "last_seen": time.time(),
        })
    return peers

def persist_peer_table():
    with peer_lock:
        state = {
            "version": 1,
            "local": dict(local_info),
            "peers": {key: value for key, value in peer_table.items()},
        }
        route_addresses = sorted({
            value["mesh_ip"]
            for value in peer_table.values()
            if value.get("mesh_ip") and value.get("mesh_ip") != local_info.get("mesh_ip")
        })
    temporary = PEER_STATE_FILE + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as state_file:
        json.dump(state, state_file)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temporary, PEER_STATE_FILE)
    routes_temp = NATS_ROUTES_FILE + ".tmp"
    fd = os.open(routes_temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as routes_file:
        routes_file.write("\n".join(route_addresses))
        if route_addresses:
            routes_file.write("\n")
        routes_file.flush()
        os.fsync(routes_file.fileno())
    os.replace(routes_temp, NATS_ROUTES_FILE)

def load_peer_table():
    try:
        with open(PEER_STATE_FILE) as state_file:
            state = json.load(state_file)
        if isinstance(state, dict):
            with peer_lock:
                if state.get("version") == 1:
                    local_info.update(state.get("local", {}))
                    peer_table.update(state.get("peers", {}))
                else:
                    peer_table.update(state)
            persist_peer_table()
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"[Mesh] Could not load peer state: {exc}", flush=True)

class JoinHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def _reply(self, code, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/join":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("invalid request size")
            body = self.rfile.read(length)
            timestamp = self.headers.get("X-Mesh-Timestamp", "")
            nonce = self.headers.get("X-Mesh-Nonce", "")
            signature = self.headers.get("X-Mesh-Signature", "")
            if abs(int(time.time()) - int(timestamp)) > 300 or not 16 <= len(nonce) <= 64:
                self._reply(401, {"error": "stale or invalid request"})
                return
            canonical = f"POST:/join:{timestamp}:{nonce}:".encode() + body
            expected = hmac.new(MESH_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                self._reply(403, {"error": "invalid join signature"})
                return
            with join_nonce_lock:
                now = time.time()
                expired = [key for key, seen in join_nonces.items() if now - seen > 360]
                for key in expired:
                    del join_nonces[key]
                nonce_key = f"{timestamp}:{nonce}"
                if nonce_key in join_nonces:
                    self._reply(409, {"error": "join request replayed"})
                    return
                join_nonces[nonce_key] = now

            request = json.loads(body)
            node_id = request.get("node_id", "")
            pubkey = request.get("pubkey", "")
            endpoint = request.get("public_endpoint", "")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", node_id):
                raise ValueError("invalid node ID")
            if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", pubkey):
                raise ValueError("invalid WireGuard public key")
            host, port = endpoint.rsplit(":", 1)
            ipaddress.ip_address(host)
            if not 1 <= int(port) <= 65535:
                raise ValueError("invalid WireGuard endpoint")

            with peer_lock:
                existing = peer_table.get(pubkey)
                assigned_ip = existing.get("mesh_ip") if existing else allocate_next_ip()
                if not assigned_ip:
                    self._reply(503, {"error": "mesh address pool exhausted"})
                    return
                peer_table[pubkey] = {
                    "node_id": node_id,
                    "mesh_ip": assigned_ip,
                    "endpoint": endpoint,
                    "wg_port": int(port),
                    "last_seen": time.time(),
                }
                roster = peer_snapshot(exclude_pubkey=pubkey)
            persist_peer_table()
            sync_peer(pubkey, assigned_ip, endpoint)
            self._reply(200, {
                "assigned_ip": assigned_ip,
                "bootstrap_mesh_ip": local_info["mesh_ip"],
                "peers": roster,
            })
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception as exc:
            print(f"[Mesh] Join request failed: {exc}", flush=True)
            self._reply(500, {"error": "join request failed"})

def join_http_server():
    ThreadingHTTPServer(("127.0.0.1", JOIN_PORT), JoinHandler).serve_forever()

def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

def init_wg_keys():
    os.makedirs("/etc/wireguard", exist_ok=True)
    priv_path = "/etc/wireguard/private.key"
    pub_path = "/etc/wireguard/public.key"

    if not os.path.exists(priv_path):
        privkey = subprocess.check_output(["wg", "genkey"], text=True).strip()
        fd = os.open(priv_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(privkey)
        pubkey = subprocess.check_output(["wg", "pubkey"], input=privkey, text=True).strip()
        with open(pub_path, "w") as f:
            f.write(pubkey)
    else:
        os.chmod(priv_path, 0o600)
        privkey = open(priv_path).read().strip()
        pubkey = subprocess.check_output(["wg", "pubkey"], input=privkey, text=True).strip()
        with open(pub_path, "w") as f:
            f.write(pubkey)

    return pubkey

def setup_wg_interface(mesh_ip):
    try:
        run_cmd(["ip", "link", "del", "dev", WG_IFACE])
    except Exception:
        pass

    run_cmd(["ip", "link", "add", "dev", WG_IFACE, "type", "wireguard"])
    run_cmd(["wg", "set", WG_IFACE, "private-key", "/etc/wireguard/private.key", "listen-port", str(WG_PORT)])
    run_cmd(["ip", "addr", "add", f"{mesh_ip}/24", "dev", WG_IFACE])
    run_cmd(["ip", "link", "set", "mtu", "1420", "up", "dev", WG_IFACE])
    print(f"[Mesh] {WG_IFACE} brought up with address {mesh_ip}/24 (Port {WG_PORT})", flush=True)

def sync_peer(pubkey, mesh_ip, endpoint=None):
    cmd = ["wg", "set", WG_IFACE, "peer", pubkey, "allowed-ips", f"{mesh_ip}/32", "persistent-keepalive", "25"]
    if endpoint and ":" in endpoint:
        cmd.extend(["endpoint", endpoint])
    try:
        run_cmd(cmd)
        print(f"[Mesh] Synced peer: {mesh_ip} ({pubkey[:10]}...)", flush=True)
    except Exception as e:
        print(f"[Mesh] Failed to sync peer {pubkey[:10]}: {e}", flush=True)

def allocate_next_ip():
    # Reserve .1 for primary bootstrap, scan from .2 onwards
    with peer_lock:
        used_ips = {info["mesh_ip"] for info in peer_table.values() if "mesh_ip" in info}
        if local_info["mesh_ip"]:
            used_ips.add(local_info["mesh_ip"])

    for host in SUBNET.hosts():
        ip_str = str(host)
        if ip_str.endswith(".0") or ip_str.endswith(".255"):
            continue
        if ip_str not in used_ips:
            return ip_str
    return None

def gossip_server(sock):
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            msg = decode_message(data)
            msg_type = msg.get("type", "HEARTBEAT")
            sender_key = msg.get("pubkey")

            if not sender_key or sender_key == local_info["pubkey"]:
                continue

            if msg_type == "DISCOVER":
                node_id = msg.get("node_id", "")
                endpoint = observed_peer_endpoint(msg, addr[0])
                with peer_lock:
                    existing = peer_table.get(sender_key)
                    assigned_ip = existing.get("mesh_ip") if existing else allocate_next_ip()
                    if assigned_ip:
                        peer_table[sender_key] = {
                            "node_id": node_id,
                            "mesh_ip": assigned_ip,
                            "endpoint": endpoint,
                            "wg_port": msg.get("wg_port", WG_PORT),
                            "last_seen": time.time(),
                        }
                    roster = peer_snapshot(exclude_pubkey=sender_key)
                if not assigned_ip:
                    continue
                persist_peer_table()
                print(f"[Mesh] Assigning {assigned_ip} to new peer {sender_key[:10]} from {addr[0]}", flush=True)
                response = {
                    "type": "OFFER",
                    "assigned_ip": assigned_ip,
                    "server_pubkey": local_info["pubkey"],
                    "server_mesh_ip": local_info["mesh_ip"],
                    "peers": roster,
                }
                sock.sendto(encode_message(response), addr)

            elif msg_type == "JOINED":
                # Node confirms its IP and presence
                assigned_ip = msg.get("mesh_ip")
                ep = observed_peer_endpoint(msg, addr[0])
                with peer_lock:
                    peer_table[sender_key] = {
                        "node_id": msg.get("node_id", ""),
                        "mesh_ip": assigned_ip,
                        "endpoint": ep,
                        "wg_port": msg.get("wg_port", WG_PORT),
                        "last_seen": time.time(),
                    }
                persist_peer_table()
                if local_info["mesh_ip"]:
                    sync_peer(sender_key, assigned_ip, ep)

            elif msg_type == "HEARTBEAT":
                ep = observed_peer_endpoint(msg, addr[0])
                sip = msg.get("mesh_ip")
                if sender_key not in peer_table:
                    with peer_lock:
                        peer_table[sender_key] = {
                            "node_id": msg.get("node_id", ""),
                            "mesh_ip": sip,
                            "endpoint": ep,
                            "wg_port": msg.get("wg_port", WG_PORT),
                            "last_seen": time.time(),
                        }
                    if local_info["mesh_ip"]:
                        sync_peer(sender_key, sip, ep)
                else:
                    with peer_lock:
                        peer_table[sender_key]["last_seen"] = time.time()
                        peer_table[sender_key]["endpoint"] = ep

                # Merge transitive peer entries
                for p in msg.get("peers", []):
                    pk = p.get("pubkey")
                    if pk and pk != local_info["pubkey"] and pk not in peer_table:
                        with peer_lock:
                            peer_table[pk] = {"mesh_ip": p["mesh_ip"], "endpoint": p.get("endpoint"), "last_seen": time.time()}
                        if local_info["mesh_ip"]:
                            sync_peer(pk, p["mesh_ip"], p.get("endpoint"))
                persist_peer_table()

        except Exception as e:
            time.sleep(0.5)

def bootstrap_join(sock, my_pubkey):
    print(f"[Mesh] Bootstrapping via {BOOTSTRAP_PEERS}...", flush=True)
    node_id = os.getenv("NODE_ID", "").strip()
    disc_payload = encode_message({
        "type": "DISCOVER",
        "node_id": node_id,
        "pubkey": my_pubkey,
        "public_endpoint": local_info["public_endpoint"],
        "wg_port": WG_PORT,
    })
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while not local_info["mesh_ip"]:
        for bootstrap in BOOTSTRAP_PEERS:
            host, port = bootstrap.rsplit(":", 1)
            try:
                sent = client_sock.sendto(disc_payload, (host, int(port)))
                print(f"[Mesh] Discovery sent to {host}:{port} ({sent} bytes)", flush=True)
            except Exception as exc:
                print(f"[Mesh] Discovery send failed: {exc}", flush=True)

        client_sock.settimeout(5.0)
        try:
            data, addr = client_sock.recvfrom(4096)
            msg = decode_message(data)
            if msg.get("type") == "OFFER" and msg.get("assigned_ip"):
                local_info["mesh_ip"] = msg["assigned_ip"]
                local_info["public_endpoint"] = public_endpoint()
                print(f"[Mesh] Successfully claimed coordinated IP: {local_info['mesh_ip']}", flush=True)

                setup_wg_interface(local_info["mesh_ip"])

                # Add offering node
                s_key = msg["server_pubkey"]
                s_ip = msg["server_mesh_ip"]
                s_ep = f"{addr[0]}:{WG_PORT}"
                peer_table[s_key] = {"mesh_ip": s_ip, "endpoint": s_ep, "last_seen": time.time()}
                sync_peer(s_key, s_ip, s_ep)

                # Add other network peers returned in the offer
                for p in msg.get("peers", []):
                    pk = p.get("pubkey")
                    if pk and pk != my_pubkey:
                        peer_table[pk] = {"mesh_ip": p["mesh_ip"], "endpoint": p.get("endpoint"), "last_seen": time.time()}
                        sync_peer(pk, p["mesh_ip"], p.get("endpoint"))

                # Announce finalized join
                joined = encode_message({
                    "type": "JOINED",
                    "node_id": node_id,
                    "pubkey": my_pubkey,
                    "mesh_ip": local_info["mesh_ip"],
                    "public_endpoint": local_info["public_endpoint"],
                    "wg_port": WG_PORT,
                })
                client_sock.sendto(joined, addr)
                persist_peer_table()
                break
        except socket.timeout:
            print("[Mesh] No discovery offer received; retrying", flush=True)
            continue
        finally:
            client_sock.settimeout(None)
    client_sock.close()

def bootstrap_join_https(sock, my_pubkey):
    node_id = os.getenv("NODE_ID", "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,64}", node_id):
        raise RuntimeError("NODE_ID must be set for mesh enrollment")
    endpoint = public_endpoint()
    body = json.dumps({
        "node_id": node_id,
        "pubkey": my_pubkey,
        "public_endpoint": endpoint,
    }, sort_keys=True, separators=(",", ":")).encode()

    while not local_info["mesh_ip"]:
        timestamp = str(int(time.time()))
        nonce = os.urandom(16).hex()
        canonical = f"POST:/join:{timestamp}:{nonce}:".encode() + body
        signature = hmac.new(MESH_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            MESH_BOOTSTRAP_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Mesh-Timestamp": timestamp,
                "X-Mesh-Nonce": nonce,
                "X-Mesh-Signature": signature,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                offer = json.loads(response.read().decode("utf-8"))
            assigned_ip = offer["assigned_ip"]
            ipaddress.ip_address(assigned_ip)
            local_info["mesh_ip"] = assigned_ip
            local_info["public_endpoint"] = endpoint
            setup_wg_interface(assigned_ip)

            for peer in offer.get("peers", []):
                if peer.get("pubkey") == my_pubkey:
                    continue
                peer_ip = peer["mesh_ip"]
                ipaddress.ip_address(peer_ip)
                peer_endpoint = peer.get("endpoint")
                peer_table[peer["pubkey"]] = {
                    "node_id": peer.get("node_id", ""),
                    "mesh_ip": peer_ip,
                    "endpoint": peer_endpoint,
                    "wg_port": peer.get("wg_port", WG_PORT),
                    "last_seen": time.time(),
                }
                sync_peer(peer["pubkey"], peer_ip, peer_endpoint)

            persist_peer_table()
            joined = encode_message({
                "type": "JOINED",
                "node_id": node_id,
                "pubkey": my_pubkey,
                "mesh_ip": assigned_ip,
                "public_endpoint": endpoint,
                "wg_port": WG_PORT,
            })
            bootstrap_mesh_ip = offer["bootstrap_mesh_ip"]
            ipaddress.ip_address(bootstrap_mesh_ip)
            sock.sendto(joined, (bootstrap_mesh_ip, MESH_PORT))
            print(f"[Mesh] HTTPS enrollment assigned {assigned_ip}; peers={len(peer_table)}", flush=True)
            return
        except Exception as exc:
            print(f"[Mesh] HTTPS enrollment failed: {exc}; retrying", flush=True)
            time.sleep(5)

def heartbeat_loop(sock, my_pubkey):
    while True:
        if local_info["mesh_ip"]:
            payload = encode_message({
                "type": "HEARTBEAT",
                "pubkey": my_pubkey,
                "mesh_ip": local_info["mesh_ip"],
                "node_id": os.getenv("NODE_ID", "sa-1-monitoring"),
                "public_endpoint": local_info["public_endpoint"],
                "wg_port": WG_PORT,
                "peers": peer_snapshot()
            })

            with peer_lock:
                peers = list(peer_table.items())
            for k, v in peers:
                if v.get("mesh_ip"):
                    try:
                        sock.sendto(payload, (v["mesh_ip"], MESH_PORT))
                    except Exception:
                        pass
        time.sleep(10)

if __name__ == "__main__":
    pubkey = init_wg_keys()
    local_info["pubkey"] = pubkey
    local_info["public_endpoint"] = public_endpoint()
    load_peer_table()
    print(f"[Mesh] Node Identity: PubKey={pubkey}", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", MESH_PORT))

    server_thread = threading.Thread(target=gossip_server, args=(sock,), daemon=True)
    server_thread.start()
    join_thread = threading.Thread(target=join_http_server, name="mesh-join-api", daemon=True)
    join_thread.start()

    # If static WG_ADDRESS is passed (e.g. Node 1 is root 10.10.0.1)
    manual_ip = os.getenv("WG_ADDRESS")
    if manual_ip:
        base_ip = manual_ip.split("/")[0]
        local_info["mesh_ip"] = base_ip
        setup_wg_interface(base_ip)
        persist_peer_table()
    elif MESH_BOOTSTRAP_URL:
        local_info["mesh_ip"] = None
        bootstrap_join_https(sock, pubkey)
    elif BOOTSTRAP_PEERS:
        local_info["mesh_ip"] = None
        bootstrap_join(sock, pubkey)
    else:
        # Default root node if no bootstrap peers given
        local_info["mesh_ip"] = "10.10.0.1"
        setup_wg_interface("10.10.0.1")
        persist_peer_table()

    heartbeat_loop(sock, pubkey)
