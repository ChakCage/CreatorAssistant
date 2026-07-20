from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from creator_assistant.infrastructure.credential_store import WindowsCredentialStore
from creator_assistant.services.publishing.http import PublishingHttpClient


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthCallback:
    def __init__(self, redirect_uri: str = "") -> None:
        self.result: dict[str, str] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.result = {key: values[0] for key, values in parse_qs(urlparse(self.path).query).items() if values}
                body = "<html><meta charset='utf-8'><body><h2>Авторизация завершена.</h2><p>Вернитесь в Creator Assistant.</p></body></html>".encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, *_args):
                return

        parsed = urlparse(redirect_uri) if redirect_uri else None
        host = parsed.hostname if parsed and parsed.hostname in {"127.0.0.1", "localhost"} else "127.0.0.1"
        port = parsed.port if parsed and parsed.port else 0
        self.server = HTTPServer((host, port), Handler)
        self.redirect_uri = redirect_uri or f"http://127.0.0.1:{self.server.server_port}/"

    def wait(self, timeout: float = 300) -> dict[str, str]:
        thread = threading.Thread(target=self.server.handle_request, daemon=True)
        thread.start(); thread.join(timeout)
        self.server.server_close()
        if thread.is_alive():
            raise TimeoutError("OAuth authorization timed out")
        return self.result


class OAuthService:
    GOOGLE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
    TIKTOK_SCOPES = ["user.info.basic", "video.upload", "video.publish"]

    def __init__(self, credentials: WindowsCredentialStore, http: PublishingHttpClient | None = None) -> None:
        self.credentials = credentials
        self.http = http or PublishingHttpClient()

    def import_google_client(self, path: Path) -> str:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        config = raw.get("installed") or raw.get("desktop")
        if not isinstance(config, dict) or not config.get("client_id"):
            raise ValueError("Нужен Google OAuth client JSON типа Desktop app")
        client_id = str(config["client_id"])
        config_id = "google-client-" + hashlib.sha256(client_id.encode()).hexdigest()[:12]
        self.credentials.write_json(config_id, {
            "client_id": client_id,
            "client_secret": str(config.get("client_secret") or ""),
            "auth_uri": str(config.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth"),
            "token_uri": str(config.get("token_uri") or "https://oauth2.googleapis.com/token"),
        }, "client")
        return config_id

    def authorize_google(self, account_id: str, config_id: str, open_browser=webbrowser.open, timeout: float = 300) -> dict[str, Any]:
        config = self.credentials.read_json(config_id, "client")
        if not config:
            raise ValueError("Google OAuth configuration is not imported")
        callback = OAuthCallback(); state = secrets.token_urlsafe(24); verifier, challenge = _pkce()
        query = urlencode({
            "client_id": config["client_id"], "redirect_uri": callback.redirect_uri,
            "response_type": "code", "scope": " ".join(self.GOOGLE_SCOPES),
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        open_browser(f"{config['auth_uri']}?{query}")
        response = callback.wait(timeout)
        if response.get("state") != state or not response.get("code"):
            raise ValueError(response.get("error") or "OAuth state/code validation failed")
        form = {
            "client_id": config["client_id"], "code": response["code"],
            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": callback.redirect_uri,
        }
        if config.get("client_secret"):
            form["client_secret"] = config["client_secret"]
        token = self.http.request("POST", config["token_uri"], headers={"Content-Type": "application/x-www-form-urlencoded"}, body=urlencode(form).encode()).json()
        token["expires_at"] = time.time() + float(token.get("expires_in", 3600))
        token["client_config_id"] = config_id
        self.credentials.write_json(account_id, token)
        return token

    def refresh_google(self, account_id: str) -> dict[str, Any]:
        token = self.credentials.read_json(account_id)
        config = self.credentials.read_json(str(token.get("client_config_id") or ""), "client")
        if not token.get("refresh_token") or not config:
            raise ValueError("Google refresh token is unavailable")
        form = {"client_id": config["client_id"], "refresh_token": token["refresh_token"], "grant_type": "refresh_token"}
        if config.get("client_secret"):
            form["client_secret"] = config["client_secret"]
        updated = self.http.request("POST", config["token_uri"], headers={"Content-Type": "application/x-www-form-urlencoded"}, body=urlencode(form).encode()).json()
        token.update(updated); token["expires_at"] = time.time() + float(updated.get("expires_in", 3600))
        self.credentials.write_json(account_id, token)
        return token

    def import_tiktok_client(self, client_key: str, client_secret: str, redirect_uri: str) -> str:
        if not client_key.strip() or not redirect_uri.strip():
            raise ValueError("TikTok client key и redirect URI обязательны")
        config_id = "tiktok-client-" + hashlib.sha256(client_key.encode()).hexdigest()[:12]
        self.credentials.write_json(config_id, {"client_key": client_key.strip(), "client_secret": client_secret, "redirect_uri": redirect_uri.strip()}, "client")
        return config_id

    def authorize_tiktok(self, account_id: str, config_id: str, open_browser=webbrowser.open, timeout: float = 300) -> dict[str, Any]:
        config = self.credentials.read_json(config_id, "client")
        if not config:
            raise ValueError("TikTok OAuth configuration is not imported")
        callback = OAuthCallback(str(config["redirect_uri"])); state = secrets.token_urlsafe(24); verifier, challenge = _pkce()
        query = urlencode({
            "client_key": config["client_key"], "scope": ",".join(self.TIKTOK_SCOPES),
            "response_type": "code", "redirect_uri": callback.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        open_browser(f"https://www.tiktok.com/v2/auth/authorize/?{query}")
        response = callback.wait(timeout)
        if response.get("state") != state or not response.get("code"):
            raise ValueError(response.get("error") or "TikTok OAuth state/code validation failed")
        form = urlencode({
            "client_key": config["client_key"], "client_secret": config.get("client_secret", ""),
            "code": response["code"], "grant_type": "authorization_code",
            "redirect_uri": callback.redirect_uri, "code_verifier": verifier,
        }).encode()
        token = self.http.request("POST", "https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=form).json()
        token["expires_at"] = time.time() + float(token.get("expires_in", 86400))
        token["client_config_id"] = config_id
        self.credentials.write_json(account_id, token)
        return token

    def refresh_tiktok(self, account_id: str) -> dict[str, Any]:
        token = self.credentials.read_json(account_id)
        config = self.credentials.read_json(str(token.get("client_config_id") or ""), "client")
        if not token.get("refresh_token") or not config:
            raise ValueError("TikTok refresh token is unavailable")
        form = urlencode({"client_key": config["client_key"], "client_secret": config.get("client_secret", ""), "grant_type": "refresh_token", "refresh_token": token["refresh_token"]}).encode()
        updated = self.http.request("POST", "https://open.tiktokapis.com/v2/oauth/token/", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=form).json()
        token.update(updated); token["expires_at"] = time.time() + float(updated.get("expires_in", 86400))
        self.credentials.write_json(account_id, token)
        return token
