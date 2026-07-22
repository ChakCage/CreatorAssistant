from __future__ import annotations

import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.security import mint_service_token  # noqa: E402


BASE = os.getenv("E2E_LICENSE_URL", "http://127.0.0.1:18080").rstrip("/")
BOT = os.getenv("E2E_BOT_URL", "http://127.0.0.1:18081").rstrip("/")
SECRET = os.environ["LICENSE_BOT_SERVICE_SECRET"]
PERMISSIONS = ["users:write", "plans:read", "checkout:create", "subscription:read", "activation:create"]


def request(method: str, url: str, value=None, *, service: bool = False, form: bool = False):
    headers = {}
    data = None
    if value is not None:
        if form:
            data = urllib.parse.urlencode(value).encode(); headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(value).encode(); headers["Content-Type"] = "application/json"
    if service:
        headers["Authorization"] = "Bearer " + mint_service_token("docker-e2e", PERMISSIONS, "creator_assistant", SECRET)
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=20) as response:
        raw = response.read().decode()
        return (response.status, raw, dict(response.headers))


def json_request(method: str, path: str, value=None, *, service: bool = True):
    status, raw, _ = request(method, BASE + path, value, service=service)
    return status, json.loads(raw)


def main() -> int:
    assert json.loads(request("GET", BASE + "/ready")[1])["status"] == "ready"
    assert json.loads(request("GET", BOT + "/ready")[1])["status"] == "ready"
    user_id = "e2e-" + secrets.token_hex(5)
    json_request("POST", "/v1/bot/users/upsert", {"telegram_user_id": user_id, "username": "docker_e2e"})
    _, plan_data = json_request("GET", "/v1/bot/plans")
    plan = plan_data["plans"][0]
    _, checkout = json_request("POST", "/v1/billing/checkout", {
        "telegram_user_id": user_id, "plan_id": plan["plan_id"], "price_id": plan["price_id"],
        "idempotency_key": "e2e-" + secrets.token_hex(10),
    })
    _, page, headers = request("GET", checkout["checkout_url"])
    csrf = re.search(r'name=csrf value="([0-9a-f]+)"', page).group(1)
    assert "default-src 'none'" in headers.get("Content-Security-Policy", page)
    request("POST", checkout["checkout_url"], {"csrf": csrf, "action": "success"}, form=True)
    _, subscription = json_request("GET", f"/v1/bot/subscription/{user_id}")
    assert subscription["status"] == "ACTIVE"
    _, activation = json_request("POST", "/v1/bot/activation-code", {"telegram_user_id": user_id})
    assert activation["activation_code"].startswith("CA-")
    if os.getenv("E2E_CODE_ONLY") == "1":
        print(activation["activation_code"])
        return 0
    print(json.dumps({
        "api": "ready", "bot": "ready", "payment_id": checkout["payment_id"],
        "subscription": subscription["status"], "activation_code_created": True,
        "one_time_checkout_consumed": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
