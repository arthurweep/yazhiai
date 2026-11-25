from __future__ import annotations

import json
import sys
import httpx
from settings import DEVICE_ID, FURNACE_URL, REQUEST_TIMEOUT_SECONDS

print('URL:', FURNACE_URL)
print('POST body:', {'deviceId': DEVICE_ID})
try:
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False) as client:
        r = client.post(FURNACE_URL, json={'deviceId': DEVICE_ID})
        print('HTTP:', r.status_code)
        r.raise_for_status()
        data = r.json()
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
except Exception as exc:
    print('FAILED:', type(exc).__name__, exc)
    sys.exit(1)
