from __future__ import annotations

import base64
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from creator_assistant.infrastructure.build_info import current_build_info
from creator_assistant.infrastructure.secure_credential_store import WindowsSecureCredentialStore
from creator_assistant.product import AppEdition


class LicenseState(str, Enum):
    ACTIVE = "ACTIVE"
    OFFLINE_GRACE = "OFFLINE_GRACE"
    SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    DEVICE_REVOKED = "DEVICE_REVOKED"
    UPDATE_REQUIRED = "UPDATE_REQUIRED"
    NOT_ACTIVATED = "NOT_ACTIVATED"


@dataclass(frozen=True)
class LicenseStatus:
    active: bool
    state: LicenseState = LicenseState.NOT_ACTIVATED
    plan: str = ""
    expires_at: str = ""
    offline_grace_until: str = ""
    last_refresh: str = ""
    device_id: str = ""
    message: str = ""
    features: tuple[str, ...] = ()
    request_id: str = ""


class LicenseClientError(RuntimeError):
    def __init__(self, code: str, message: str = "", request_id: str = "", details: dict | None = None) -> None:
        super().__init__(message or code); self.code = code; self.request_id = request_id; self.details = details or {}


@dataclass(frozen=True)
class LicenseRequestDiagnostics:
    edition: str
    channel: str
    hostname: str
    method: str = ""
    path: str = ""
    timeout_seconds: int = 0
    http_status: int = 0
    request_id: str = ""
    error_code: str = ""

    def safe_text(self) -> str:
        return (
            f"Edition: {self.edition} · Channel: {self.channel}\n"
            f"API: {self.hostname}{self.path}\n"
            f"Метод: {self.method or '—'} · Timeout: {self.timeout_seconds or '—'} с · "
            f"HTTP: {self.http_status or '—'}\n"
            f"Код: {self.error_code or '—'} · Request ID: {self.request_id or '—'}"
        )


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _version(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.split("."):
        try: parts.append(int("".join(ch for ch in item if ch.isdigit()) or 0))
        except ValueError: parts.append(0)
    return tuple(parts)


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SecureLicenseStorage:
    def __init__(self, credentials: WindowsSecureCredentialStore | None = None) -> None:
        self.credentials = credentials or WindowsSecureCredentialStore(namespace="CreatorAssistant/Commercial/License")

    def load(self) -> dict[str, Any]:
        state = self.credentials.read_json("license-state", kind="metadata")
        entitlement = self.credentials.read_json("license-entitlement", kind="token")
        refresh = self.credentials.read_json("license-refresh", kind="credential")
        if entitlement.get("value"):
            state["entitlement_token"] = entitlement["value"]
        if refresh.get("value"):
            state["refresh_credential"] = refresh["value"]
        if state:
            return state
        # One-time compatibility with the early development layout.
        return self.credentials.read_json("license", kind="entitlement")

    def save(self, value: dict[str, Any]) -> None:
        state = {key: item for key, item in value.items() if key not in {"entitlement_token", "refresh_credential"}}
        self.credentials.write_json("license-entitlement", {"value": value.get("entitlement_token", "")}, kind="token")
        self.credentials.write_json("license-refresh", {"value": value.get("refresh_credential", "")}, kind="credential")
        self.credentials.write_json("license-state", state, kind="metadata")
        self.credentials.delete("license", kind="entitlement")

    def clear(self) -> None:
        for item_id, kind in (("license-entitlement", "token"), ("license-refresh", "credential"),
                              ("license-state", "metadata"), ("license", "entitlement")):
            self.credentials.delete(item_id, kind=kind)

    def installation_id(self) -> str:
        value = self.credentials.read_json("installation", kind="id")
        if value.get("installation_id"): return str(value["installation_id"])
        installation_id = str(uuid.uuid4())
        self.credentials.write_json("installation", {"installation_id": installation_id}, kind="id")
        return installation_id


class LicenseService:
    """Commercial entitlement client. Developer callers never instantiate or contact it."""

    def __init__(self, storage: SecureLicenseStorage | None = None, *, endpoint: str | None = None,
                 public_keys: dict[str, str] | None = None, opener: Callable[..., Any] | None = None,
                 wall_clock: Callable[[], datetime] | None = None) -> None:
        build = current_build_info()
        if storage is None:
            namespace = ("CreatorAssistant/CommercialStaging/License"
                         if build.license_backend_profile == "staging"
                         else "CreatorAssistant/Commercial/License")
            if (
                build.license_backend_profile == "local"
                or (build.license_backend_profile == "staging" and os.environ.get("CREATOR_ASSISTANT_E2E") == "1")
            ):
                namespace = os.environ.get("CREATOR_ASSISTANT_E2E_CREDENTIAL_NAMESPACE", namespace)
            storage = SecureLicenseStorage(WindowsSecureCredentialStore(namespace=namespace))
        self.storage = storage
        self.endpoint = (endpoint or build.license_backend_url).rstrip("/")
        self.public_keys = dict(public_keys if public_keys is not None else build.license_public_keys)
        self.opener = opener or urllib.request.urlopen
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.process_started_monotonic = time.monotonic()
        self.process_started_wall = self.wall_clock()
        self.last_request_id = ""
        self.last_error_code = ""
        split = urlsplit(self.endpoint)
        self.last_diagnostics = LicenseRequestDiagnostics(
            edition=build.edition,
            channel=build.channel,
            hostname=split.hostname or "",
        )

    @property
    def installation_id(self) -> str: return self.storage.installation_id()

    def _request(self, method: str, path: str, payload: dict | None = None, credential: str = "", timeout: int = 20) -> dict:
        if not self.endpoint.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise LicenseClientError("INVALID_BACKEND_PROFILE", "Недопустимый адрес сервера лицензий")
        headers = {"Content-Type": "application/json", "User-Agent": f"CreatorAssistant/{current_build_info().version}"}
        if credential: headers["Authorization"] = "Bearer " + credential
        request = urllib.request.Request(self.endpoint + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None, headers=headers, method=method)
        build = current_build_info()
        split = urlsplit(self.endpoint)
        self.last_diagnostics = LicenseRequestDiagnostics(
            edition=build.edition, channel=build.channel, hostname=split.hostname or "",
            method=method, path=path, timeout_seconds=timeout,
        )
        try:
            with self.opener(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                status = int(getattr(response, "status", 0) or getattr(response, "getcode", lambda: 0)() or 0)
                request_id = str(getattr(response, "headers", {}).get("X-Request-ID", "") or "")
                self.last_request_id = request_id
                self.last_error_code = ""
                self.last_diagnostics = LicenseRequestDiagnostics(
                    edition=build.edition, channel=build.channel, hostname=split.hostname or "",
                    method=method, path=path, timeout_seconds=timeout, http_status=status,
                    request_id=request_id,
                )
        except urllib.error.HTTPError as exc:
            try: body = json.loads(exc.read().decode("utf-8"))
            except Exception: body = {}
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = str(error.get("code") or f"HTTP_{exc.code}")
            request_id = str(
                body.get("request_id", "")
                or getattr(exc, "headers", {}).get("X-Request-ID", "")
                or ""
            )
            self.last_request_id = request_id
            self.last_error_code = code
            self.last_diagnostics = LicenseRequestDiagnostics(
                edition=build.edition, channel=build.channel, hostname=split.hostname or "",
                method=method, path=path, timeout_seconds=timeout, http_status=int(exc.code),
                request_id=request_id, error_code=code,
            )
            message = _friendly_error(code)
            if not message and isinstance(body, dict):
                message = str(body.get("detail") or "")
            raise LicenseClientError(
                code, message or f"Сервер лицензий вернул HTTP {exc.code}.",
                request_id=request_id, details=error,
            ) from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            self._record_error("TLS_ERROR", method, path, timeout, build, split)
            raise LicenseClientError("TLS_ERROR", "Не удалось проверить защищённое соединение с сервером лицензий.") from exc
        except (socket.timeout, TimeoutError) as exc:
            self._record_error("REQUEST_TIMEOUT", method, path, timeout, build, split)
            raise LicenseClientError("REQUEST_TIMEOUT", f"Сервер лицензий не ответил за {timeout} с.") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "REQUEST_TIMEOUT" if isinstance(reason, (socket.timeout, TimeoutError)) else "SERVER_UNAVAILABLE"
            self._record_error(code, method, path, timeout, build, split)
            message = f"Сервер лицензий не ответил за {timeout} с." if code == "REQUEST_TIMEOUT" else "Нет соединения с сервером лицензий."
            raise LicenseClientError(code, message) from exc
        except OSError as exc:
            self._record_error("SERVER_UNAVAILABLE", method, path, timeout, build, split)
            raise LicenseClientError("SERVER_UNAVAILABLE", "Нет соединения с сервером лицензий.") from exc
        if not isinstance(result, dict):
            self._record_error(
                "INVALID_SERVER_RESPONSE", method, path, timeout, build, split,
                http_status=self.last_diagnostics.http_status, request_id=self.last_diagnostics.request_id,
            )
            raise LicenseClientError("INVALID_SERVER_RESPONSE", "Сервер лицензий вернул некорректный ответ.",
                                     request_id=self.last_request_id)
        return result

    def _record_error(
        self, code: str, method: str, path: str, timeout: int, build, split,
        *, http_status: int = 0, request_id: str = "",
    ) -> None:
        self.last_error_code = code
        self.last_request_id = request_id
        self.last_diagnostics = LicenseRequestDiagnostics(
            edition=build.edition, channel=build.channel, hostname=split.hostname or "",
            method=method, path=path, timeout_seconds=timeout, http_status=http_status,
            request_id=request_id, error_code=code,
        )

    def _verify(self, token: str) -> dict[str, Any]:
        try:
            prefix, key_id, payload_text, signature_text = token.split(".", 3)
            if prefix != "ca1" or key_id not in self.public_keys: raise ValueError("unknown key")
            raw = _unb64(payload_text)
            Ed25519PublicKey.from_public_bytes(_unb64(self.public_keys[key_id])).verify(_unb64(signature_text), raw)
            payload = json.loads(raw)
        except Exception as exc: raise LicenseClientError("INVALID_ENTITLEMENT_SIGNATURE", "Подпись лицензии повреждена") from exc
        if payload.get("schema_version") != 1: raise LicenseClientError("UNSUPPORTED_ENTITLEMENT_SCHEMA")
        if payload.get("installation_id") != self.installation_id: raise LicenseClientError("ENTITLEMENT_DEVICE_MISMATCH")
        if payload.get("product_code") != "creator_assistant": raise LicenseClientError("ENTITLEMENT_PRODUCT_MISMATCH")
        return payload

    def activate(self, activation_code: str, device_name: str = "") -> LicenseStatus:
        build = current_build_info()
        result = self._request("POST", "/v1/licenses/activate", {
            "activation_code": activation_code, "installation_id": self.installation_id,
            "device_name": device_name or platform.node() or "Windows PC", "os_version": platform.platform(),
            "app_version": build.version, "edition": "commercial",
        }, timeout=30)
        payload = self._verify(str(result.get("entitlement_token", "")))
        self._persist_result(result, payload)
        return self.status()

    def _persist_result(self, result: dict, payload: dict) -> None:
        # Token, refresh credential and timing evidence remain together in Credential Manager only.
        self.storage.save({"entitlement_token": result["entitlement_token"],
                           "refresh_credential": result.get("refresh_credential", ""),
                           "payload": payload, "subscription": result.get("subscription", {}),
                           "device": result.get("device", {}), "last_known_server_time": result.get("server_time", ""),
                           "last_refresh": self.wall_clock().isoformat(), "last_wall_time": self.wall_clock().isoformat()})

    def refresh(self) -> LicenseStatus:
        stored = self.storage.load(); credential = str(stored.get("refresh_credential", ""))
        if not credential: raise LicenseClientError("NOT_ACTIVATED")
        result = self._request("POST", "/v1/licenses/refresh", {"refresh_credential": credential,
            "installation_id": self.installation_id, "app_version": current_build_info().version}, timeout=30)
        payload = self._verify(str(result.get("entitlement_token", ""))); self._persist_result(result, payload)
        return self.status()

    def refresh_if_due(self, hours: int = 24) -> bool:
        stored = self.storage.load()
        if not stored.get("refresh_credential"):
            return False
        last_refresh = str(stored.get("last_refresh", ""))
        if last_refresh and self.wall_clock() - _parse_time(last_refresh) < __import__("datetime").timedelta(hours=hours):
            return False
        self.refresh()
        return True

    def status(self, *, server_available: bool | None = None) -> LicenseStatus:
        stored = self.storage.load()
        if not stored.get("entitlement_token"): return LicenseStatus(False, LicenseState.NOT_ACTIVATED, message="Требуется активация подписки", request_id=self.last_request_id)
        try: payload = self._verify(str(stored["entitlement_token"]))
        except LicenseClientError as exc: return LicenseStatus(False, LicenseState.BLOCKED, message=str(exc))
        now = self.wall_clock(); last_wall = stored.get("last_wall_time", "")
        if last_wall and now < _parse_time(str(last_wall)) - __import__("datetime").timedelta(minutes=10):
            return LicenseStatus(False, LicenseState.SERVER_UNAVAILABLE, message="Обнаружен значительный откат системного времени; требуется онлайн-проверка")
        monotonic_expected = self.process_started_wall + __import__("datetime").timedelta(seconds=max(0.0, time.monotonic() - self.process_started_monotonic))
        if now < monotonic_expected - __import__("datetime").timedelta(minutes=10):
            return LicenseStatus(False, LicenseState.SERVER_UNAVAILABLE, message="Системное время изменилось во время работы; требуется онлайн-проверка")
        if now < _parse_time(str(payload["not_before"])) - __import__("datetime").timedelta(minutes=2):
            return self._status(payload, False, LicenseState.BLOCKED, "Лицензионный токен ещё не действует")
        minimum = str(payload.get("minimum_app_version") or "")
        if minimum and _version(current_build_info().version) < _version(minimum):
            return self._status(payload, False, LicenseState.UPDATE_REQUIRED, "Требуется обновление приложения")
        if now > _parse_time(str(payload["subscription_until"])):
            return self._status(payload, False, LicenseState.EXPIRED, "Подписка закончилась. Ваши проекты и готовые файлы сохранены. Продлите доступ, чтобы продолжить обработку")
        if now <= _parse_time(str(payload["expires_at"])):
            return self._status(payload, True, LicenseState.ACTIVE)
        if now <= _parse_time(str(payload["offline_grace_until"])):
            return self._status(payload, True, LicenseState.OFFLINE_GRACE, "Сервер недоступен: используется временный offline-доступ")
        return self._status(payload, False, LicenseState.SERVER_UNAVAILABLE if server_available is False else LicenseState.EXPIRED,
                            "Offline-период закончился; подключитесь к Интернету")

    def _status(self, payload: dict, active: bool, state: LicenseState, message: str = "") -> LicenseStatus:
        stored = self.storage.load()
        return LicenseStatus(active, state, str(payload.get("plan_code", "")), str(payload.get("subscription_until", "")),
            str(payload.get("offline_grace_until", "")), str(stored.get("last_refresh", "")), str(payload.get("device_id", "")),
            message, tuple(str(item) for item in payload.get("features", [])), self.last_request_id)

    def devices(self) -> list[dict]:
        stored = self.storage.load(); result = self._request("GET", "/v1/licenses/devices", credential=str(stored.get("refresh_credential", "")))
        return list(result.get("devices", []))

    def deactivate_device(self, device_id: str) -> None:
        stored = self.storage.load(); self._request("POST", f"/v1/licenses/devices/{device_id}/deactivate", {"reason": "user_request"}, str(stored.get("refresh_credential", "")))
        if device_id == str((stored.get("device") or {}).get("id", "")): self.storage.clear()

    def logout(self) -> None:
        stored = self.storage.load()
        if stored.get("refresh_credential"):
            try: self._request("POST", "/v1/licenses/revoke-local", {}, str(stored["refresh_credential"]))
            except LicenseClientError: pass
        self.storage.clear()


class FeatureGate:
    def __init__(self, edition: AppEdition, license_service: LicenseService | None = None) -> None:
        self.edition = edition; self.license_service = license_service

    def status(self) -> LicenseStatus:
        if self.edition is AppEdition.DEVELOPER: return LicenseStatus(True, LicenseState.ACTIVE, features=("*",))
        return self.license_service.status() if self.license_service else LicenseStatus(False)

    def require(self, feature: str) -> None:
        if self.edition is AppEdition.DEVELOPER: return
        status = self.status()
        if not status.active or feature not in status.features:
            raise LicenseClientError(status.state.value, status.message or "Для этой операции нужна активная подписка")


def _friendly_error(code: str) -> str:
    return {
        "INVALID_ACTIVATION_CODE": "Код активации не найден или введён неверно",
        "ACTIVATION_CODE_EXPIRED": "Срок действия кода активации истёк",
        "ACTIVATION_CODE_USED": "Этот код активации уже использован",
        "ACTIVATION_CODE_REVOKED": "Этот код активации отозван",
        "TOO_MANY_ATTEMPTS": "Слишком много попыток. Повторите позже",
        "SUBSCRIPTION_INACTIVE": "Подписка неактивна",
        "DEVICE_LIMIT_REACHED": "Достигнут лимит устройств. Отключите старое устройство",
        "DEVICE_REVOKED": "Это устройство отключено или заблокировано",
        "REFRESH_SESSION_INVALID": "Сеанс лицензии отозван. Выполните активацию снова",
    }.get(code, "Сервер лицензий отклонил запрос")
