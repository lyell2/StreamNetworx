import hashlib
import hmac
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

path = f"/api/v1/stations/{STATION}/suspend"
timestamp = str(int(time.time()))
nonce = str(uuid.uuid4())
body = ""
canonical = f"POST:{path}:{timestamp}:{nonce}:{body}"
signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
headers = {
    "X-API-Key": KEY_ID,
    "X-Timestamp": timestamp,
    "X-Nonce": nonce,
    "X-Signature": signature,
}

request = urllib.request.Request(f"{API_URL}{path}", headers=headers, method="POST")
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("Suspend HTTP Status:", response.status)
        print(response.read().decode())
except urllib.error.HTTPError as error:
    raise SystemExit(f"Control API returned {error.code}: {error.read().decode(errors='replace')}")
