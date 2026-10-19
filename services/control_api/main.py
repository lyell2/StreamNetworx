import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from pathlib import Path

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header, Depends, status
from fastapi.responses import JSONResponse
import redis.asyncio as redis
from pydantic import BaseModel
from typing import Optional
import secrets
import httpx

NODE_ID = os.getenv("NODE_ID", "sa-1-monitoring")
MESH_SECRET = os.getenv("MESH_INTERNAL_SECRET", "")
MESH_STATE_FILE = Path(os.getenv("MESH_STATE_FILE", "/app/mesh/peers.json"))
MESH_GATEWAY_URL = os.getenv("MESH_GATEWAY_URL", "http://stream-node:5060").rstrip("/")
if len(MESH_SECRET) < 32:
    raise RuntimeError("MESH_INTERNAL_SECRET must be set to at least 32 characters")

redis_client = redis.from_url(
    os.getenv("REDIS_URL", "redis://redis-hub:6379/1"),
    decode_responses=True,
)
mesh_http_client = None


def mesh_peer_addresses():
    try:
        with MESH_STATE_FILE.open() as state_file:
            state = json.load(state_file)
        peers = state.get("peers", {}) if state.get("version") == 1 else state
        local_ip = state.get("local", {}).get("mesh_ip") if state.get("version") == 1 else None
        addresses = set()
        for peer in peers.values():
            address = peer.get("mesh_ip")
            if address:
                parsed = ipaddress.ip_address(address)
                if parsed in ipaddress.ip_network("10.10.0.0/24") and address != local_ip:
                    addresses.add(str(parsed))
        return sorted(addresses)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def verify_mesh_secret(secret):
    if not MESH_SECRET or not hmac.compare_digest(secret, MESH_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized mesh peer")


async def merge_station_state(record):
    station_id = record.get("station_id", "")
    metadata = record.get("metadata")
    if not STATION_PATTERN.fullmatch(station_id) or not isinstance(metadata, dict):
        return False
    try:
        incoming_version = (int(metadata["updated_ms"]), str(metadata["origin_node"]))
    except (KeyError, TypeError, ValueError):
        return False
    key = f"streamnet:station:{station_id}:meta"
    current = await redis_client.hgetall(key)
    try:
        current_version = (int(current.get("updated_ms", "0")), str(current.get("origin_node", "")))
    except ValueError:
        current_version = (0, "")
    if incoming_version <= current_version:
        return False
    allowed_fields = {"station", "mount", "billing_status", "studio_password", "updated_ms", "origin_node"}
    safe_mapping = {field: str(value) for field, value in metadata.items() if field in allowed_fields}
    await redis_client.hset(key, mapping=safe_mapping)
    return True


async def get_station_snapshot():
    stations = []
    async for key in redis_client.scan_iter(match="streamnet:station:*:meta"):
        parts = key.split(":")
        if len(parts) != 4 or not STATION_PATTERN.fullmatch(parts[2]):
            continue
        metadata = await redis_client.hgetall(key)
        if metadata:
            stations.append({"station_id": parts[2], "metadata": metadata})
    return stations


async def station_sync_loop():
    while True:
        try:
            for peer_ip in mesh_peer_addresses():
                url = f"{MESH_GATEWAY_URL}/internal/mesh/{peer_ip}/control/stations/snapshot"
                response = await mesh_http_client.get(
                    url,
                    headers={"X-Mesh-Secret": MESH_SECRET},
                    timeout=5.0,
                )
                if response.status_code == 200:
                    for record in response.json().get("stations", []):
                        await merge_station_state(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[MESH_STATE_SYNC] {type(exc).__name__}", flush=True)
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app):
    global mesh_http_client
    mesh_http_client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=2.0))
    sync_task = asyncio.create_task(station_sync_loop())
    try:
        yield
    finally:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        await mesh_http_client.aclose()
        await redis_client.aclose()


app = FastAPI(lifespan=lifespan)
CLIENTS_FILE = Path(os.getenv("CLIENTS_FILE", "/app/secrets/clients.json"))
STATION_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")


@app.get("/internal/stations/snapshot")
async def station_snapshot(x_mesh_secret: str = Header(..., alias="X-Mesh-Secret")):
    verify_mesh_secret(x_mesh_secret)
    return {"node_id": NODE_ID, "stations": await get_station_snapshot()}


def load_client_registry():
    try:
        with CLIENTS_FILE.open() as registry_file:
            registry = json.load(registry_file)
        if not isinstance(registry, dict) or not registry:
            raise ValueError("empty or invalid registry")
        return registry
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Control API client registry unavailable",
        ) from exc


async def verify_control_request(
    request: Request,
    x_api_key: str = Header(..., max_length=64),
    x_timestamp: str = Header(..., max_length=16),
    x_nonce: str = Header(..., min_length=16, max_length=64),
    x_signature: str = Header(..., min_length=64, max_length=64),
):
    registry = load_client_registry()
    caller = registry.get(x_api_key)
    if not caller:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    try:
        timestamp = int(x_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed timestamp") from exc
    if abs(int(time.time()) - timestamp) > 300:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Timestamp expired")

    try:
        accepted = await redis_client.set(f"control_nonce:{x_api_key}:{x_nonce}", "1", ex=360, nx=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Control API replay protection unavailable",
        ) from exc
    if not accepted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay detected")

    body = (await request.body()).decode("utf-8", errors="strict")
    canonical = f"{request.method.upper()}:{request.url.path}:{x_timestamp}:{x_nonce}:{body}"
    expected = hmac.new(caller["secret"].encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signature authentication failed")
    return {"key_id": x_api_key, **caller}


def require_network_admin(caller):
    if caller.get("role") != "network_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Network administrator role required")


def require_station_access(station_id, caller):
    if not STATION_PATTERN.fullmatch(station_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid station identifier")
    allowed = caller.get("allowed_stations", [])
    if caller.get("role") != "network_admin" and station_id not in allowed and "*" not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Station access denied")

@app.post("/api/v1/auth")
async def mediamtx_auth_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    action = data.get("action", "")
    path = data.get("path", "").strip("/")
    user = data.get("user", "")
    password = data.get("password", "")
    query = data.get("query", "")

    if action != "publish":
        return JSONResponse(status_code=200, content={"status": "authorized"})

    station_id = path.replace("live/", "").strip("/")
    if not station_id:
        raise HTTPException(status_code=400, detail="Missing station path")

    meta = await redis_client.hgetall(f"streamnet:station:{station_id}:meta")
    if not meta:
        raise HTTPException(status_code=404, detail="Station not found")

    billing_status = meta.get("billing_status") or meta.get("status")
    if billing_status != "active":
        raise HTTPException(status_code=403, detail="Station account suspended")

    expected_pass = meta.get("studio_password") or meta.get("password")
    candidates = [p for p in [password, user, query] if p]

    if not expected_pass or expected_pass not in candidates:
        raise HTTPException(status_code=401, detail="Invalid studio credentials")

    return JSONResponse(status_code=200, content={"status": "authorized"})

class StationCreate(BaseModel):
    station_id: str
    password: Optional[str] = None

@app.post("/api/v1/stations")
@app.post("/stations")
async def provision_station(req: StationCreate, caller: dict = Depends(verify_control_request)):
    require_network_admin(caller)
    if not STATION_PATTERN.fullmatch(req.station_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid station identifier")
    password = req.password or secrets.token_urlsafe(12)

    await redis_client.hset(f"streamnet:station:{req.station_id}:meta", mapping={
        "station": req.station_id,
        "mount": f"/{req.station_id}.mp3",
        "billing_status": "active",
        "studio_password": password,
        "updated_ms": int(time.time() * 1000),
        "origin_node": NODE_ID,
    })

    return {
        "status": "provisioned",
        "station_id": req.station_id,
        "password": password,
        "ingest_url": f"srt://127.0.0.1:6000?streamid=publish:{req.station_id}:{password}"
    }

@app.post("/api/v1/stations/{station_id}/activate")
@app.post("/stations/{station_id}/activate")
async def activate_station(station_id: str, caller: dict = Depends(verify_control_request)):
    require_station_access(station_id, caller)
    key = f"streamnet:station:{station_id}:meta"
    metadata = await redis_client.hgetall(key)
    if not metadata:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    updated_ms = int(time.time() * 1000)
    await redis_client.hset(key, mapping={
        "billing_status": "active",
        "updated_ms": updated_ms,
        "origin_node": NODE_ID,
    })
    return {"status": "active", "station": station_id, "updated_ms": updated_ms}

@app.post("/api/v1/stations/{station_id}/suspend")
@app.post("/stations/{station_id}/suspend")
async def suspend_station(station_id: str, caller: dict = Depends(verify_control_request)):
    require_station_access(station_id, caller)
    await redis_client.hset(f"streamnet:station:{station_id}:meta", mapping={
        "billing_status": "suspended",
        "updated_ms": int(time.time() * 1000),
        "origin_node": NODE_ID,
    })

    dropped = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://stream-node:9997/v3/paths/list")
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                for item in items:
                    if item.get("name") == station_id:
                        source = item.get("source", {})
                        if source.get("type") == "srtConn" and source.get("id"):
                            source_id = source.get("id")
                            # Kick the exact socket!
                            await client.post(f"http://stream-node:9997/v3/srtconns/kick/{source_id}")
                            dropped = True
    except Exception as e:
        print(f"[KICK_ERROR] {e}")

    return {
        "status": "suspended",
        "station": station_id,
        "active_stream_dropped": dropped
    }
