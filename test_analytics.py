import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
import uuid


API_URL = os.getenv("ANALYTICS_API_URL", "https://stream.streammedia.org.za:8080/api/v1/analytics")
KEY_ID = os.getenv("ANALYTICS_API_KEY_ID", "")
SECRET = os.getenv("ANALYTICS_API_SECRET", "")
STATION = os.getenv("ANALYTICS_TEST_STATION", "")


def request(path, payload):
    if not KEY_ID or not SECRET or not STATION:
        raise RuntimeError("Set ANALYTICS_API_KEY_ID, ANALYTICS_API_SECRET, and ANALYTICS_TEST_STATION")

    body = json.dumps(payload, separators=(",", ":"))
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    full_path = f"/api/v1/analytics{path}"
    canonical = f"POST:{full_path}:{timestamp}:{nonce}:{body}"
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-API-Key": KEY_ID,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"{API_URL}{path}", data=body.encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Analytics API returned {error.code}: {detail}") from error


def main():
    summary_path = f"/stations/{STATION}/metrics/summary"
    status_code, summary = request(summary_path, {
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
    })
    print(f"Summary endpoint: HTTP {status_code}; sessions={summary['data']['total_sessions']}")

    token_path = f"/stations/{STATION}/tokens/mint"
    status_code, token = request(token_path, {
        "mount": f"/{STATION}.mp3",
        "duration_seconds": 60,
    })
    print(f"Token mint endpoint: HTTP {status_code}; expiry={token['data']['expires_at']}")


if __name__ == "__main__":
    main()
