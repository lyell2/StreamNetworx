import os, time, hmac, hashlib, json, asyncio, re, logging, urllib.parse, uuid
import ipaddress
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Generic, TypeVar
from datetime import date, datetime

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request, Depends, status, Path as FPath
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("streamnetworx.api")

# --- Environment & Secret Validation ---
CLICKHOUSE_URL = os.getenv("CLICKHOUSE_HTTP", "http://clickhouse-hub:8123").rstrip("/")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASS = os.getenv("CLICKHOUSE_PASS", "")
REDIS_URL = os.getenv("REDIS_URL", "")

NODE_ID = os.getenv("NODE_ID", "sa-1-monitoring").strip()
MESH_ADVERTISED_URL = os.getenv("MESH_ADVERTISED_URL", "http://127.0.0.1:5050").rstrip("/")
MESH_SECRET = os.getenv("MESH_INTERNAL_SECRET", "").strip()

if len(MESH_SECRET) < 32:
    raise RuntimeError("MESH_INTERNAL_SECRET must be set to at least 32 characters")
if not CLICKHOUSE_PASS or not REDIS_URL:
    raise RuntimeError("CLICKHOUSE_PASS and REDIS_URL must be configured")

raw_peers = os.getenv("INTERNAL_MESH_PEERS", "")
INTERNAL_PEERS = [p.strip().rstrip("/") for p in raw_peers.split(",") if p.strip()]
MESH_STATE_FILE = Path(os.getenv("MESH_STATE_FILE", "/app/mesh/peers.json"))
MESH_GATEWAY_URL = os.getenv("MESH_GATEWAY_URL", "http://stream-node:5050").rstrip("/")

def mesh_peer_addresses() -> List[str]:
    try:
        with MESH_STATE_FILE.open() as state_file:
            state = json.load(state_file)
        peers = state.get("peers", {}) if state.get("version") == 1 else state
        local_address = state.get("local", {}).get("mesh_ip") if state.get("version") == 1 else None
        addresses = set()
        for peer in peers.values():
            address = peer.get("mesh_ip")
            if address:
                parsed = ipaddress.ip_address(address)
                if parsed in ipaddress.ip_network("10.10.0.0/24") and address != local_address:
                    addresses.add(str(parsed))
        return sorted(addresses)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []

CLIENTS_FILE = Path(os.getenv("CLIENTS_FILE", "/app/secrets/clients.json"))

_clients_cache: Dict[str, Any] = {}
_clients_last_mtime: float = 0.0

STATION_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{2,64}$")

def validate_station_name(station: str) -> str:
    if not STATION_REGEX.match(station):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid station identifier format. Must be 2-64 alphanumeric characters, hyphens, or underscores."
        )
    return station

def load_client_registry() -> Dict[str, Any]:
    global _clients_cache, _clients_last_mtime
    try:
        mtime = CLIENTS_FILE.stat().st_mtime
        if mtime > _clients_last_mtime:
            with open(CLIENTS_FILE, "r") as f:
                _clients_cache = json.load(f)
            _clients_last_mtime = mtime
    except Exception as exc:
        if not _clients_cache:
            raise RuntimeError(f"Could not load tenant registry: {exc}")
    return _clients_cache

http_client: Optional[httpx.AsyncClient] = None
redis_client: Optional[aioredis.Redis] = None

class RingTopology:
    def __init__(self, my_url: str, peer_urls: List[str]):
        self.my_url = my_url
        self.all_urls = sorted(list(set([my_url] + peer_urls)))
        self.primary_buddy: Optional[str] = None
        self.fallback_buddy: Optional[str] = None
        self.current_target: Optional[str] = None
        self._calculate_buddies()

    def update_peers(self, peer_urls: List[str]):
        self.all_urls = sorted(set([self.my_url] + peer_urls))
        self._calculate_buddies()

    def _calculate_buddies(self):
        if len(self.all_urls) <= 1:
            self.primary_buddy = None
            self.fallback_buddy = None
            self.current_target = None
            return

        idx = self.all_urls.index(self.my_url)
        n = len(self.all_urls)
        self.primary_buddy = self.all_urls[(idx + 1) % n]
        self.fallback_buddy = self.all_urls[(idx + 2) % n] if n > 2 else self.primary_buddy
        self.current_target = self.primary_buddy

    async def poll_health_and_elect(self, client: httpx.AsyncClient):
        if not self.primary_buddy:
            return

        is_primary_alive = False
        try:
            resp = await client.get(f"{self.primary_buddy}/healthz", timeout=1.5)
            if resp.status_code == 200:
                is_primary_alive = True
        except Exception:
            is_primary_alive = False

        if not is_primary_alive and self.fallback_buddy:
            self.current_target = self.fallback_buddy
        else:
            self.current_target = self.primary_buddy

ring_manager = RingTopology(MESH_ADVERTISED_URL, INTERNAL_PEERS)

async def ring_background_worker():
    while True:
        try:
            peer_urls = [
                f"{MESH_GATEWAY_URL}/internal/mesh/{peer_ip}"
                for peer_ip in mesh_peer_addresses()
            ]
            ring_manager.update_peers(peer_urls)
            if http_client:
                await ring_manager.poll_health_and_elect(http_client)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"Topology health check tick exception: {exc}")
        await asyncio.sleep(10.0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, redis_client
    load_client_registry()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout=10.0, connect=2.5),
        limits=httpx.Limits(max_keepalive_connections=25, max_connections=100)
    )
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2.0)
    worker_task = asyncio.create_task(ring_background_worker())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await http_client.aclose()
    await redis_client.aclose()

app = FastAPI(
    title="StreamNetworx Edge API",
    version="3.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan
)

# --- DTOs & Validation Models ---
T = TypeVar("T")

class ResponseEnvelope(BaseModel, Generic[T]):
    success: bool = True
    station: str
    is_complete: bool = True
    nodes_reporting: List[str] = Field(default_factory=list)
    nodes_offline: List[str] = Field(default_factory=list)
    data: T
    execution_time_ms: float

class MountMetric(BaseModel):
    mount: str
    total_sessions: int
    unique_listeners: int
    listening_hours: float
    bandwidth_gb: float
    avg_duration_seconds: float

class StationSummary(BaseModel):
    start_date: date
    end_date: date
    total_sessions: int
    unique_listeners: int
    listening_hours: float
    bandwidth_gb: float
    avg_duration_seconds: float
    mounts: List[MountMetric]

class SummaryFilter(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be greater than or equal to start_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("Query window cannot exceed 366 days in a single request")
        return self

class MintTokenRequest(BaseModel):
    mount: str = Field(..., min_length=2, max_length=128)
    duration_seconds: int = Field(default=86400, ge=60, le=2592000)
    client_ip: Optional[str] = Field(default=None, max_length=45)

    @field_validator("mount")
    @classmethod
    def sanitize_mount(cls, v: str) -> str:
        v = "/" + v.strip().lstrip("/")
        if any(c in v for c in ("'", '"', ";", "--", "/*", "*/", "\\")):
            raise ValueError("Invalid mount name syntax")
        return v

class MintTokenResponse(BaseModel):
    station: str
    mount: str
    token: str
    expires_at: int
    playback_query_param: str
    sample_playback_url: str

class SessionIngestRecord(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)
    node_id: str = Field(..., min_length=1, max_length=64)
    timestamp_ms: int = Field(..., ge=0)
    client_ip: str = Field(..., max_length=45)
    mount: str = Field(..., min_length=1, max_length=128)
    status: int = Field(..., ge=100, le=599)
    duration: float = Field(..., ge=0.0)
    bytes_sent: int = Field(..., ge=0)
    user_agent: str = Field(default="", max_length=512)

class CallerIdentity(BaseModel):
    key_id: str
    role: str
    allowed_stations: List[str]

# --- Auth & Middleware ---
async def verify_request(
    request: Request,
    x_api_key: str = Header(..., max_length=64),
    x_timestamp: str = Header(..., max_length=16),
    x_nonce: str = Header(..., max_length=64),
    x_signature: str = Header(..., max_length=64)
) -> CallerIdentity:
    registry = load_client_registry()
    client = registry.get(x_api_key)
    if not client:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    try:
        req_ts = int(x_timestamp)
        if abs(int(time.time()) - req_ts) > 300:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Timestamp expired (skew > 300s)")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed timestamp format")

    nonce_key = f"api_nonce:{x_api_key}:{x_nonce}"
    try:
        nonce_set = await redis_client.set(nonce_key, "1", ex=360, nx=True)
    except Exception as exc:
        logger.error(f"Redis cache failure during nonce check: {exc}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication verification service degraded")

    if not nonce_set:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Replay attack detected: Nonce already used")

    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8", errors="ignore")
    p_raw = request.url.path
    candidates = [p_raw, f"/api/v1/analytics{p_raw}" if not p_raw.startswith("/api/v1/analytics") else p_raw]
    secret = client["secret"]

    valid = any(
        hmac.compare_digest(
            hmac.new(secret.encode(), f"{request.method.upper()}:{p}:{x_timestamp}:{x_nonce}:{body_str}".encode(), hashlib.sha256).hexdigest(),
            x_signature
        ) for p in candidates
    )
    if not valid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Signature authentication failed")
    return CallerIdentity(key_id=x_api_key, role=client["role"], allowed_stations=client["allowed_stations"])

def verify_internal_mesh(x_mesh_secret: str = Header(...)):
    if not hmac.compare_digest(x_mesh_secret, MESH_SECRET):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized mesh node connection")

def assert_station_access(station: str, caller: CallerIdentity):
    if "*" not in caller.allowed_stations and station not in caller.allowed_stations:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Access forbidden: Station '{station}' unauthorized for tenant")

def sign_playback_token(secret: str, key_id: str, station: str, expires_at: int, client_ip: str) -> str:
    message = f"v1:{key_id}:{station}:{expires_at}:{client_ip}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

# --- ClickHouse Client Driver ---
async def execute_clickhouse(sql: str, params: Dict[str, Any]) -> Dict[str, Any]:
    query_params = {
        "user": CLICKHOUSE_USER,
        "password": CLICKHOUSE_PASS,
        "default_format": "JSON",
        "readonly": "1",
        "max_execution_time": "10"
    }
    for k, v in params.items():
        query_params[f"param_{k}"] = str(v)
    try:
        resp = await http_client.post(f"{CLICKHOUSE_URL}/", params=query_params, content=sql.encode("utf-8"))
        if resp.status_code != 200:
            logger.error(f"ClickHouse execution error: {resp.text.strip()}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Analytics database query execution failure")
        return resp.json()
    except httpx.RequestError as exc:
        logger.error(f"ClickHouse communication error: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Analytics database unreachable")

async def get_locally_stored_node_ids() -> List[str]:
    sql = "SELECT DISTINCT node_id FROM radio_analytics.stream_sessions_raw"
    res = await execute_clickhouse(sql, {})
    return [r["node_id"] for r in res.get("data", [])]

async def fetch_local_station_summary(station: str, payload: SummaryFilter, target_node_id: str) -> Dict[str, Any]:
    sql = """
    SELECT
        mount,
        count() AS total_sessions,
        uniqExact(client_ip) AS unique_listeners,
        round(sum(duration) / 3600, 2) AS listening_hours,
        round(sum(bytes_sent) / (1024 * 1024 * 1024), 2) AS bandwidth_gb,
        round(avg(duration), 1) AS avg_duration_seconds
    FROM radio_analytics.stream_sessions_raw FINAL
    WHERE node_id = {target_node_id:String}
      AND mount LIKE concat('%', {station:String}, '%')
      AND toDate(timestamp_ms / 1000) >= {start_date:Date}
      AND toDate(timestamp_ms / 1000) <= {end_date:Date}
      AND status IN (200, 206)
    GROUP BY mount
    ORDER BY listening_hours DESC
    """
    params = {"target_node_id": target_node_id, "station": station, "start_date": str(payload.start_date), "end_date": str(payload.end_date)}
    res = await execute_clickhouse(sql, params)
    mounts = [MountMetric(**r) for r in res.get("data", [])]

    total_sessions = sum(m.total_sessions for m in mounts)
    total_tlh = round(sum(m.listening_hours for m in mounts), 2)
    total_gb = round(sum(m.bandwidth_gb for m in mounts), 2)
    avg_duration = round(sum(m.avg_duration_seconds * m.total_sessions for m in mounts) / total_sessions, 1) if total_sessions > 0 else 0.0

    unique_sql = """
    SELECT uniqExact(client_ip) AS total_uniques
    FROM radio_analytics.stream_sessions_raw FINAL
    WHERE node_id = {target_node_id:String}
      AND mount LIKE concat('%', {station:String}, '%')
      AND toDate(timestamp_ms / 1000) >= {start_date:Date}
      AND toDate(timestamp_ms / 1000) <= {end_date:Date}
      AND status IN (200, 206)
    """
    uniques_res = await execute_clickhouse(unique_sql, params)
    total_uniques = uniques_res.get("data", [{}])[0].get("total_uniques", 0)

    return {
        "node_id": target_node_id,
        "summary": StationSummary(
            start_date=payload.start_date,
            end_date=payload.end_date,
            total_sessions=total_sessions,
            unique_listeners=total_uniques,
            listening_hours=total_tlh,
            bandwidth_gb=total_gb,
            avg_duration_seconds=avg_duration,
            mounts=mounts
        )
    }

async def fetch_remote_peer_summary(peer_url: str, station: str, payload: SummaryFilter) -> Optional[Dict[str, Any]]:
    try:
        url = f"{MESH_GATEWAY_URL}/internal/mesh/{peer_url}/metrics/summary"
        headers = {"X-Mesh-Secret": MESH_SECRET, "Content-Type": "application/json"}
        resp = await http_client.post(url, params={"station": station}, content=payload.model_dump_json().encode(), headers=headers, timeout=2.5)
        if resp.status_code == 200:
            data = resp.json()
            data["peer_url"] = peer_url
            return data
    except Exception as exc:
        logger.debug(f"Peer query timeout or unreachable: {peer_url} ({exc})")
    return None

def merge_summaries(items: List[Dict[str, Any]], start_date: date, end_date: date) -> StationSummary:
    mount_map: Dict[str, Dict[str, Any]] = {}
    total_sessions = 0
    total_uniques = 0
    total_tlh = 0.0
    total_gb = 0.0
    weighted_duration_sum = 0.0

    for item in items:
        s = item["summary"]
        if isinstance(s, dict):
            s = StationSummary(**s)
        total_sessions += s.total_sessions
        total_uniques += s.unique_listeners
        total_tlh += s.listening_hours
        total_gb += s.bandwidth_gb
        weighted_duration_sum += s.avg_duration_seconds * s.total_sessions

        for m in s.mounts:
            if m.mount not in mount_map:
                mount_map[m.mount] = {"total_sessions": 0, "unique_listeners": 0, "listening_hours": 0.0, "bandwidth_gb": 0.0, "weighted_dur": 0.0}
            mount_map[m.mount]["total_sessions"] += m.total_sessions
            mount_map[m.mount]["unique_listeners"] += m.unique_listeners
            mount_map[m.mount]["listening_hours"] += m.listening_hours
            mount_map[m.mount]["bandwidth_gb"] += m.bandwidth_gb
            mount_map[m.mount]["weighted_dur"] += m.avg_duration_seconds * m.total_sessions

    merged_mounts: List[MountMetric] = []
    for mount_name, d in mount_map.items():
        dur = round(d["weighted_dur"] / d["total_sessions"], 1) if d["total_sessions"] > 0 else 0.0
        merged_mounts.append(MountMetric(
            mount=mount_name,
            total_sessions=d["total_sessions"],
            unique_listeners=d["unique_listeners"],
            listening_hours=round(d["listening_hours"], 2),
            bandwidth_gb=round(d["bandwidth_gb"], 2),
            avg_duration_seconds=dur
        ))

    overall_avg = round(weighted_duration_sum / total_sessions, 1) if total_sessions > 0 else 0.0
    return StationSummary(
        start_date=start_date,
        end_date=end_date,
        total_sessions=total_sessions,
        unique_listeners=total_uniques,
        listening_hours=round(total_tlh, 2),
        bandwidth_gb=round(total_gb, 2),
        avg_duration_seconds=overall_avg,
        mounts=sorted(merged_mounts, key=lambda x: x.listening_hours, reverse=True)
    )

# --- Application Endpoints ---

@app.get("/healthz")
async def health():
    peer_ips = mesh_peer_addresses()
    return {
        "status": "ok",
        "node_id": NODE_ID,
        "advertised_url": MESH_ADVERTISED_URL,
        "pool_active": http_client is not None,
        "configured_peers": peer_ips,
        "ring": ring_manager.all_urls,
        "current_target": ring_manager.current_target,
    }

@app.get("/internal/mesh/topology")
async def get_mesh_topology(_: None = Depends(verify_internal_mesh)):
    peer_ips = mesh_peer_addresses()
    ring_manager.update_peers([f"{MESH_GATEWAY_URL}/internal/mesh/{peer_ip}" for peer_ip in peer_ips])
    return {
        "node_id": NODE_ID,
        "my_url": ring_manager.my_url,
        "ring": ring_manager.all_urls,
        "primary_buddy": ring_manager.primary_buddy,
        "fallback_buddy": ring_manager.fallback_buddy,
        "current_target": ring_manager.current_target
    }

# --- Nginx auth_request Listener Validation Hook ---
@app.api_route("/webhook/auth", methods=["GET", "HEAD"])
async def authenticate_listener(
    request: Request,
    x_original_uri: Optional[str] = Header(None, alias="X-Original-URI"),
    x_real_ip: Optional[str] = Header(None, alias="X-Real-IP"),
    x_user_agent: Optional[str] = Header(None, alias="X-User-Agent")
):
    uri = x_original_uri or "/"
    parsed = urllib.parse.urlparse(uri)
    query_params = urllib.parse.parse_qs(parsed.query)

    token = query_params.get("token", [None])[0]
    exp = query_params.get("exp", [None])[0]
    uid = query_params.get("uid", [query_params.get("lid", ["anon"])])[0]
    sid = query_params.get("sid", [str(uuid.uuid4())[:8]])[0]

    # Validate token expiration if provided
    if exp:
        try:
            if int(exp) < int(time.time()):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token Expired")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Expiry Timestamp")

    # Track active concurrent session in Redis
    if redis_client:
        try:
            await redis_client.hset(f"active_session:{sid}", mapping={
                "ip": x_real_ip or "unknown",
                "mount": parsed.path,
                "start": int(time.time()),
                "uid": str(uid),
                "ua": (x_user_agent or "")[:128]
            })
            await redis_client.expire(f"active_session:{sid}", 86400)
        except Exception as exc:
            logger.debug(f"Redis session tracking error: {exc}")

    return Response(
        content="OK",
        media_type="text/plain",
        headers={
            "X-User-ID": str(uid),
            "X-Session-ID": str(sid),
            "icecast-auth-user": "1"
        }
    )

@app.post("/internal/telemetry/ingest")
async def ingest_replicated_telemetry(
    records: List[SessionIngestRecord],
    _: None = Depends(verify_internal_mesh)
):
    if len(records) > 5000:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Batch exceeds limit of 5000 records")
    if not records:
        return {"inserted": 0}
    ndjson = "\n".join(r.model_dump_json() for r in records)
    try:
        resp = await http_client.post(
            f"{CLICKHOUSE_URL}/",
            params={
                "user": CLICKHOUSE_USER,
                "password": CLICKHOUSE_PASS,
                "query": "INSERT INTO radio_analytics.stream_sessions_raw FORMAT JSONEachRow"
            },
            content=ndjson.encode("utf-8")
        )
        if resp.status_code != 200:
            logger.error(f"ClickHouse batch ingest failure: {resp.text.strip()}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Replication storage write error")
        return {"status": "replicated", "count": len(records)}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

@app.post("/internal/metrics/summary")
async def internal_summary(
    payload: SummaryFilter,
    station: str,
    _: None = Depends(verify_internal_mesh)
):
    validate_station_name(station)
    res = await fetch_local_station_summary(station, payload, target_node_id=NODE_ID)
    return {"node_id": NODE_ID, "summary": res["summary"].model_dump()}

@app.post("/webhook/listener")
@app.post("/stations/{station}/webhook/listener")
async def ingest_station_listener_webhook(request: Request, station: str = "pukfm"):
    form = await request.form()
    action = form.get("action", "listener_add")
    session_id = str(form.get("client", "unknown"))
    client_ip = str(form.get("ip", "0.0.0.0"))
    user_agent = str(form.get("agent", ""))
    duration = float(form.get("duration", 0.0))

    timestamp_ms = int(time.time() * 1000)

    ndjson = json.dumps({
        "session_id": session_id,
        "node_id": NODE_ID,
        "timestamp_ms": timestamp_ms,
        "client_ip": client_ip,
        "mount": f"/{station}",
        "status": 200 if action == "listener_add" else 206,
        "duration": duration,
        "bytes_sent": 0,
        "user_agent": user_agent
    })

    try:
        resp = await http_client.post(
            f"{CLICKHOUSE_URL}/",
            params={
                "user": CLICKHOUSE_USER,
                "password": CLICKHOUSE_PASS,
                "query": "INSERT INTO radio_analytics.stream_sessions_raw FORMAT JSONEachRow"
            },
            content=ndjson.encode("utf-8")
        )
        if resp.status_code != 200:
            logger.error(f"ClickHouse webhook ingest failure: {resp.text.strip()}")
    except Exception as exc:
        logger.error(f"Webhook exception: {exc}")

    return Response(content="ok", media_type="text/plain", headers={"icecast-auth-user": "1"})

@app.post("/stations/{station}/tokens/mint", response_model=ResponseEnvelope[MintTokenResponse])
async def mint_playback_token(
    payload: MintTokenRequest,
    station: str,
    caller: CallerIdentity = Depends(verify_request)
):
    validate_station_name(station)
    assert_station_access(station, caller)
    clean_mount = "/" + payload.mount.lstrip("/")
    expires_at = int(time.time()) + payload.duration_seconds
    ip_bind = payload.client_ip.strip() if payload.client_ip else ""
    secret = load_client_registry()[caller.key_id]["secret"]
    token_sig = sign_playback_token(secret, caller.key_id, station, expires_at, ip_bind)
    query = {"st": station, "exp": expires_at, "kid": caller.key_id, "token": token_sig}
    if ip_bind:
        query["ip"] = ip_bind
    q = urllib.parse.urlencode(query)
    return ResponseEnvelope(
        success=True,
        station=station,
        nodes_reporting=[f"{NODE_ID} (authoritative)"],
        data=MintTokenResponse(
            station=station,
            mount=clean_mount,
            token=token_sig,
            expires_at=expires_at,
            playback_query_param=q,
            sample_playback_url=f"/geo/{station}.mp3?{q}"
        ),
        execution_time_ms=0.5
    )

@app.post("/stations/{station}/metrics/summary", response_model=ResponseEnvelope[StationSummary])
async def get_summary(
    payload: SummaryFilter,
    station: str,
    caller: CallerIdentity = Depends(verify_request)
):
    validate_station_name(station)
    assert_station_access(station, caller)
    t0 = time.perf_counter()

    local_task = fetch_local_station_summary(station, payload, target_node_id=NODE_ID)
    peer_ips = mesh_peer_addresses()
    ring_manager.update_peers([f"{MESH_GATEWAY_URL}/internal/mesh/{peer_ip}" for peer_ip in peer_ips])
    peer_tasks = [fetch_remote_peer_summary(peer, station, payload) for peer in peer_ips]
    all_responses = await asyncio.gather(local_task, *peer_tasks, return_exceptions=True)

    valid_results = []
    authoritative_nodes = set()
    nodes_reporting = []
    responding_peer_urls = set()

    for r in all_responses:
        if isinstance(r, dict) and "summary" in r:
            valid_results.append(r)
            nid = r.get("node_id", "unknown")
            authoritative_nodes.add(nid)
            nodes_reporting.append(f"{nid} (authoritative)")
            if "peer_url" in r:
                responding_peer_urls.add(r["peer_url"])

    local_stored_nids = await get_locally_stored_node_ids()
    missing_nids = [nid for nid in local_stored_nids if nid not in authoritative_nodes]

    for missing_nid in missing_nids:
        escrow_res = await fetch_local_station_summary(station, payload, target_node_id=missing_nid)
        if escrow_res["summary"].total_sessions > 0:
            valid_results.append(escrow_res)
            nodes_reporting.append(f"{missing_nid} (local replica escrow)")

    nodes_offline = [p for p in peer_ips if p not in responding_peer_urls]
    merged_data = merge_summaries(valid_results, payload.start_date, payload.end_date)

    return ResponseEnvelope(
        success=True,
        station=station,
        is_complete=True,
        nodes_reporting=nodes_reporting,
        nodes_offline=nodes_offline,
        data=merged_data,
        execution_time_ms=round((time.perf_counter() - t0) * 1000, 2)
    )

from fastapi import Header, Query, HTTPException, Response

@app.get("/api/v1/auth/verify")
async def verify_stream_access(
    token: Optional[str] = Query(None),
    st: Optional[str] = Query(None),
    exp: Optional[str] = Query(None),
    kid: Optional[str] = Query(None),
    ip: Optional[str] = Query(None),
    x_real_ip: Optional[str] = Header(None, alias="X-Real-IP"),
    x_original_uri: Optional[str] = Header(None, alias="X-Original-URI")
):
    original_uri = urllib.parse.urlparse(x_original_uri or "")
    original_params = urllib.parse.parse_qs(original_uri.query)
    token = token or original_params.get("token", [None])[0]
    st = st or original_params.get("st", [None])[0]
    exp = exp or original_params.get("exp", [None])[0]
    kid = kid or original_params.get("kid", [None])[0]
    ip = ip or original_params.get("ip", [""])[0]
    route_match = re.fullmatch(r"/geo/([a-zA-Z0-9_-]+)\.mp3", original_uri.path)

    if not token or not st or not exp or not kid or not route_match:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if st != route_match.group(1):
        raise HTTPException(status_code=403, detail="Token station mismatch")

    try:
        expires_at = int(exp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid expiry timestamp")
    if expires_at < int(time.time()):
        raise HTTPException(status_code=403, detail="Token expired")

    client = load_client_registry().get(kid)
    if not client:
        raise HTTPException(status_code=401, detail="Unknown playback key")
    if "*" not in client.get("allowed_stations", []) and st not in client.get("allowed_stations", []):
        raise HTTPException(status_code=403, detail="Station access denied")
    if ip and x_real_ip and not hmac.compare_digest(ip, x_real_ip):
        raise HTTPException(status_code=403, detail="Client IP mismatch")

    expected = sign_playback_token(client["secret"], kid, st, expires_at, ip)
    if not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=401, detail="Invalid playback token")

    listener_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    return Response(status_code=200, headers={"X-Listener-ID": listener_id, "X-Session-ID": session_id})
