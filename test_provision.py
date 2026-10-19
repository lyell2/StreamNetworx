import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid

API_URL = os.getenv("CONTROL_API_URL", "https://stream.streammedia.org.za:8080/api/v1/control")
KEY_ID = os.getenv("CONTROL_API_KEY_ID", "")
SECRET = os.getenv("CONTROL_API_SECRET", "")
STATION = os.getenv("CONTROL_TEST_STATION", "")
if not all((KEY_ID, SECRET, STATION)):
    raise SystemExit("Set CONTROL_API_KEY_ID, CONTROL_API_SECRET, and CONTROL_TEST_STATION locally")

payload = {"station_id": STATION}

body = json.dumps(payload, separators=(",", ":"))
path = "/api/v1/stations"
ts = str(int(time.time()))
nonce = str(uuid.uuid4())
canonical = f"POST:{path}:{ts}:{nonce}:{body}"
sig = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()

headers = {
    "X-API-Key": KEY_ID,
    "X-Timestamp": ts,
    "X-Nonce": nonce,
    "X-Signature": sig,
    "Content-Type": "application/json"
}

req = urllib.request.Request(
    f"{API_URL}/api/v1/control{path}",
    data=body.encode(),
    headers=headers,
    method="POST"
)

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        print("Provision HTTP Status:", resp.getcode())
        print("Provisioned station:", data.get("station_id"))
        print("Credential keys returned:", ", ".join(sorted(data.keys())))
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.read().decode()}")
