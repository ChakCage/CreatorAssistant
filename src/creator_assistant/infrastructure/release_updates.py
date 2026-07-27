from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class UpdateError(RuntimeError):
    """A safe, user-facing update failure."""

    def __init__(self, code: str, message: str, *, request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    edition: str
    channel: str
    version: str
    build_number: int
    minimum_supported_version: str
    architecture: str
    download_url: str
    sha256: str
    file_size: int
    release_notes: str
    published_at: str
    mandatory: bool
    key_id: str
    signature: str

    @classmethod
    def from_dict(cls, value: dict) -> "ReleaseManifest":
        required = {field.name for field in cls.__dataclass_fields__.values()}
        missing = sorted(required - set(value))
        if missing:
            raise UpdateError("INVALID_MANIFEST", "В манифесте обновления отсутствуют поля: " + ", ".join(missing))
        try:
            manifest = cls(**{key: value[key] for key in required})
        except (TypeError, ValueError) as exc:
            raise UpdateError("INVALID_MANIFEST", "Манифест обновления имеет неверный формат.") from exc
        if manifest.schema_version != 1:
            raise UpdateError("UNSUPPORTED_MANIFEST", "Эта версия приложения не поддерживает формат обновления.")
        if len(manifest.sha256) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in manifest.sha256):
            raise UpdateError("INVALID_MANIFEST", "SHA-256 в манифесте имеет неверный формат.")
        if manifest.file_size <= 0:
            raise UpdateError("INVALID_MANIFEST", "Размер установщика в манифесте некорректен.")
        return manifest

    def signed_payload(self) -> bytes:
        value = asdict(self)
        value.pop("signature")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(payload: dict, private_key_b64: str, key_id: str) -> dict:
    """Offline release helper. The private key is never used by the desktop app."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    unsigned = dict(payload)
    unsigned.update({"key_id": key_id, "signature": ""})
    manifest = ReleaseManifest.from_dict(unsigned)
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    unsigned["signature"] = base64.b64encode(private.sign(manifest.signed_payload())).decode("ascii")
    return unsigned


class UpdatePolicy:
    def __init__(
        self,
        *,
        edition: str,
        channel: str,
        architecture: str,
        current_version: str,
        current_build: int,
        public_keys: dict[str, str],
        allowed_hosts: tuple[str, ...] = (),
        allow_local_http: bool = False,
    ) -> None:
        self.edition = edition
        self.channel = channel
        self.architecture = architecture
        self.current_version = current_version
        self.current_build = current_build
        self.public_keys = dict(public_keys)
        self.allowed_hosts = tuple(host.casefold() for host in allowed_hosts)
        self.allow_local_http = allow_local_http

    def validate(self, manifest: ReleaseManifest, *, allow_downgrade: bool = False) -> None:
        if manifest.edition != self.edition:
            raise UpdateError("WRONG_EDITION", "Обновление предназначено для другой редакции.")
        if manifest.channel != self.channel:
            raise UpdateError("WRONG_CHANNEL", "Обновление относится к другому каналу.")
        if manifest.architecture.casefold() != self.architecture.casefold():
            raise UpdateError("WRONG_ARCHITECTURE", "Установщик предназначен для другой архитектуры.")
        self._validate_url(manifest.download_url)
        key = self.public_keys.get(manifest.key_id)
        if not key:
            raise UpdateError("UNKNOWN_UPDATE_KEY", "Манифест подписан недоверенным ключом обновлений.")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
                base64.b64decode(manifest.signature), manifest.signed_payload()
            )
        except (ValueError, InvalidSignature) as exc:
            raise UpdateError("INVALID_SIGNATURE", "Подпись манифеста обновления недействительна.") from exc
        if not allow_downgrade and _version_key(manifest.version) < _version_key(self.current_version):
            raise UpdateError("DOWNGRADE_BLOCKED", "Установка более старой версии запрещена.")
        if not allow_downgrade and manifest.version == self.current_version and manifest.build_number < self.current_build:
            raise UpdateError("DOWNGRADE_BLOCKED", "Установка более старой сборки запрещена.")

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (self.allow_local_http and local):
            raise UpdateError("INSECURE_URL", "Обновления разрешено скачивать только по HTTPS.")
        if self.allowed_hosts and str(parsed.hostname or "").casefold() not in self.allowed_hosts:
            raise UpdateError("UNTRUSTED_HOST", "Источник обновления не входит в список доверенных.")


class ReleaseUpdateService:
    def __init__(
        self,
        endpoint: str,
        policy: UpdatePolicy,
        *,
        busy_check: Callable[[], tuple[bool, str]] | None = None,
        timeout: int = 30,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.policy = policy
        self.busy_check = busy_check or (lambda: (False, ""))
        self.timeout = timeout

    def latest(self) -> ReleaseManifest | None:
        query = urllib.parse.urlencode({
            "edition": self.policy.edition,
            "channel": self.policy.channel,
            "current_version": self.policy.current_version,
            "architecture": self.policy.architecture,
        })
        url = f"{self.endpoint}/v1/releases/latest?{query}"
        self.policy._validate_url(url)
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CreatorAssistant-Updater"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                request_id = response.headers.get("X-Request-ID", "")
                value = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise UpdateError("UPDATE_CHECK_FAILED", "Не удалось проверить обновления.") from exc
        if not value:
            return None
        manifest = ReleaseManifest.from_dict(value)
        try:
            self.policy.validate(manifest)
        except UpdateError as exc:
            if not exc.request_id:
                exc.request_id = request_id
            raise
        return manifest

    def download(
        self,
        manifest: ReleaseManifest,
        destination: Path | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        self.policy.validate(manifest)
        busy, operation = self.busy_check()
        if busy:
            raise UpdateError("BUSY", f"Дождитесь завершения операции «{operation}» или отмените её вручную.")
        root = destination or Path(tempfile.gettempdir()) / "CreatorAssistant" / "updates"
        root.mkdir(parents=True, exist_ok=True)
        final = root / Path(urllib.parse.urlparse(manifest.download_url).path).name
        partial = final.with_suffix(final.suffix + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with urllib.request.urlopen(manifest.download_url, timeout=max(60, self.timeout)) as response, partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, manifest.file_size)
        except Exception as exc:
            raise UpdateError("DOWNLOAD_FAILED", "Загрузка обновления прервана; текущая версия осталась без изменений.") from exc
        if received != manifest.file_size:
            partial.unlink(missing_ok=True)
            raise UpdateError("SIZE_MISMATCH", "Размер загруженного установщика не совпадает с манифестом.")
        if digest.hexdigest().casefold() != manifest.sha256.casefold():
            partial.unlink(missing_ok=True)
            raise UpdateError("HASH_MISMATCH", "SHA-256 загруженного установщика не совпадает с манифестом.")
        os.replace(partial, final)
        return final


def _version_key(value: str) -> tuple[int, int, int, tuple]:
    main, _, suffix = str(value).partition("-")
    parts = main.split(".")
    if len(parts) != 3 or not all(item.isdigit() for item in parts):
        raise UpdateError("INVALID_VERSION", f"Некорректная версия: {value}")
    # A final build sorts above a prerelease of the same version.
    return int(parts[0]), int(parts[1]), int(parts[2]), (() if suffix else (1,))
