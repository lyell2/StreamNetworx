import os
import sys
import json
import time
import socket
import subprocess
import threading
import ipaddress

MESH_PORT = int(os.getenv("MESH_GOSSIP_PORT", "9111"))
WG_IFACE = "wg0"
WG_PORT = int(os.getenv("WG_PORT", "51820"))
raw_bp = os.getenv("BOOTSTRAP_PEERS", "").strip()
BOOTSTRAP_PEERS = [p.strip() for p in raw_bp.split(",") if p.strip() and ":" in p]
SUBNET = ipaddress.ip_network("10.10.0.0/24")

# Local state
peer_table = {}  # pubkey -> {"mesh_ip": str, "endpoint": str, "last_seen": float}
local_info = {"pubkey": None, "mesh_ip": None}

def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)

def init_wg_keys():
    os.makedirs("/etc/wireguard", exist_ok=True)
    priv_path = "/etc/wireguard/private.key"
    pub_path = "/etc/wireguard/public.key"
    
    if not os.path.exists(priv_path):
        privkey = subprocess.check_output(["wg", "genkey"], text=True).strip()
        with open(priv_path, "w") as f:
            f.write(privkey)
        pubkey = subprocess.check_output(["wg", "pubkey"], input=privkey, text=True).strip()
        with open(pub_path, "w") as f:
            f.write(pubkey)
    else:
        with open(pub_path, "r") as f:
            pubkey = f.read().strip()
            
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
            msg = json.loads(data.decode("utf-8"))
            msg_type = msg.get("type", "HEARTBEAT")
            sender_key = msg.get("pubkey")

            if not sender_key or sender_key == local_info["pubkey"]:
                continue

            if msg_type == "DISCOVER":
                # A joining node requests an IP
                assigned_ip = allocate_next_ip()
                print(f"[Mesh] Assigning {assigned_ip} to new peer {sender_key[:10]} from {addr[0]}", flush=True)
                
                response = {
                    "type": "OFFER",
                    "assigned_ip": assigned_ip,
                    "server_pubkey": local_info["pubkey"],
                    "server_mesh_ip": local_info["mesh_ip"],
                    "peers": [{"pubkey": k, **v} for k, v in peer_table.items()]
                }
                sock.sendto(json.dumps(response).encode("utf-8"), addr)

            elif msg_type == "JOINED":
                # Node confirms its IP and presence
                assigned_ip = msg.get("mesh_ip")
                ep = f"{addr[0]}:{msg.get('wg_port', WG_PORT)}"
                peer_table[sender_key] = {"mesh_ip": assigned_ip, "endpoint": ep, "last_seen": time.time()}
                if local_info["mesh_ip"]:
                    sync_peer(sender_key, assigned_ip, ep)

            elif msg_type == "HEARTBEAT":
                ep = f"{addr[0]}:{msg.get('wg_port', WG_PORT)}"
                sip = msg.get("mesh_ip")
                if sender_key not in peer_table:
                    peer_table[sender_key] = {"mesh_ip": sip, "endpoint": ep, "last_seen": time.time()}
                    if local_info["mesh_ip"]:
                        sync_peer(sender_key, sip, ep)
                else:
                    peer_table[sender_key]["last_seen"] = time.time()

                # Merge transitive peer entries
                for p in msg.get("peers", []):
                    pk = p.get("pubkey")
                    if pk and pk != local_info["pubkey"] and pk not in peer_table:
                        peer_table[pk] = {"mesh_ip": p["mesh_ip"], "endpoint": p.get("endpoint"), "last_seen": time.time()}
                        if local_info["mesh_ip"]:
                            sync_peer(pk, p["mesh_ip"], p.get("endpoint"))

        except Exception as e:
            time.sleep(0.5)

def bootstrap_join(sock, my_pubkey):
    print(f"[Mesh] Bootstrapping via {BOOTSTRAP_PEERS}...", flush=True)
    disc_payload = json.dumps({"type": "DISCOVER", "pubkey": my_pubkey, "wg_port": WG_PORT}).encode("utf-8")
    
    while not local_info["mesh_ip"]:
        for b in BOOTSTRAP_PEERS:
            host, port = b.split(":")
            try:
                sock.sendto(disc_payload, (host, int(port)))
            except Exception:
                pass
        
        sock.settimeout(2.0)
        try:
            data, addr = sock.recvfrom(4096)
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "OFFER" and msg.get("assigned_ip"):
                local_info["mesh_ip"] = msg["assigned_ip"]
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
                joined = json.dumps({"type": "JOINED", "pubkey": my_pubkey, "mesh_ip": local_info["mesh_ip"], "wg_port": WG_PORT}).encode("utf-8")
                sock.sendto(joined, addr)
                break
        except socket.timeout:
            continue
        finally:
            sock.settimeout(None)

def heartbeat_loop(sock, my_pubkey):
    while True:
        if local_info["mesh_ip"]:
            payload = json.dumps({
                "type": "HEARTBEAT",
                "pubkey": my_pubkey,
                "mesh_ip": local_info["mesh_ip"],
                "wg_port": WG_PORT,
                "peers": [{"pubkey": k, **v} for k, v in peer_table.items()]
            }).encode("utf-8")

            for target in BOOTSTRAP_PEERS:
                h, p = target.split(":")
                try:
                    sock.sendto(payload, (h, int(p)))
                except Exception:
                    pass

            for k, v in list(peer_table.items()):
                if v.get("endpoint") and ":" in v["endpoint"]:
                    h, p = v["endpoint"].split(":")
                    try:
                        sock.sendto(payload, (h, MESH_PORT))
                    except Exception:
                        pass
        time.sleep(10)

if __name__ == "__main__":
    pubkey = init_wg_keys()
    local_info["pubkey"] = pubkey
    print(f"[Mesh] Node Identity: PubKey={pubkey}", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", MESH_PORT))

    server_thread = threading.Thread(target=gossip_server, args=(sock,), daemon=True)
    server_thread.start()

    # If static WG_ADDRESS is passed (e.g. Node 1 is root 10.10.0.1)
    manual_ip = os.getenv("WG_ADDRESS")
    if manual_ip:
        base_ip = manual_ip.split("/")[0]
        local_info["mesh_ip"] = base_ip
        setup_wg_interface(base_ip)
    elif BOOTSTRAP_PEERS:
        bootstrap_join(sock, pubkey)
    else:
        # Default root node if no bootstrap peers given
        local_info["mesh_ip"] = "10.10.0.1"
        setup_wg_interface("10.10.0.1")

    heartbeat_loop(sock, pubkey)
