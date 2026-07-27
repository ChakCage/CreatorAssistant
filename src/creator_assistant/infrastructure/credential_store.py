from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from typing import Any


class CredentialStoreError(RuntimeError):
    pass


class WindowsCredentialStore:
    """Small generic-credential wrapper; secrets never touch project/settings JSON."""

    legacy_prefix = "CreatorAssistant/Publishing"
    _test_memory: dict[str, str] = {}

    def __init__(self, allow_test_memory: bool = False, namespace: str | None = None) -> None:
        self.allow_test_memory = allow_test_memory
        if namespace:
            self.prefix = namespace.rstrip("/")
        else:
            from creator_assistant.product import current_distribution_profile

            self.prefix = f"{current_distribution_profile().credential_namespace}/Publishing"

    def target(self, account_id: str, kind: str = "oauth") -> str:
        safe = "".join(ch for ch in str(account_id) if ch.isalnum() or ch in "-_.")
        return f"{self.prefix}/{safe}/{kind}"

    def write_json(self, account_id: str, value: dict[str, Any], kind: str = "oauth") -> str:
        target = self.target(account_id, kind)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self._write(target, payload)
        return target

    def read_json(self, account_id: str, kind: str = "oauth") -> dict[str, Any]:
        payload = self._read(self.target(account_id, kind))
        if not payload and self.prefix.endswith("/Developer/Publishing"):
            safe = "".join(ch for ch in str(account_id) if ch.isalnum() or ch in "-_.")
            legacy_target = f"{self.legacy_prefix}/{safe}/{kind}"
            payload = self._read(legacy_target)
            if payload:
                self._write(self.target(account_id, kind), payload)
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise CredentialStoreError("Credential payload is invalid") from exc
        return value if isinstance(value, dict) else {}

    def delete(self, account_id: str, kind: str = "oauth") -> None:
        self._delete(self.target(account_id, kind))

    def _write(self, target: str, secret: str) -> None:
        if self.allow_test_memory:
            self._test_memory[target] = secret
            return
        if os.name != "nt":
            raise CredentialStoreError("Windows Credential Manager is unavailable")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        raw = secret.encode("utf-16-le")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = CREDENTIALW(0, 1, target, "Creator Assistant publishing OAuth", wintypes.FILETIME(), len(raw), blob, 2, 0, None, None, "CreatorAssistant")
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialStoreError(f"CredWrite failed: {ctypes.get_last_error()}")

    def _read(self, target: str) -> str:
        if self.allow_test_memory:
            return self._test_memory.get(target, "")
        if os.name != "nt":
            return ""
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        pointer = ctypes.POINTER(CREDENTIALW)()
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        advapi32.CredReadW.restype = wintypes.BOOL
        if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return ""
            raise CredentialStoreError(f"CredRead failed: {ctypes.get_last_error()}")
        try:
            size = pointer.contents.CredentialBlobSize
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
            return raw.decode("utf-16-le")
        finally:
            advapi32.CredFree(pointer)

    def _delete(self, target: str) -> None:
        if self.allow_test_memory:
            self._test_memory.pop(target, None)
            return
        if os.name != "nt":
            self._test_memory.pop(target, None)
            return
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        if not advapi32.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialStoreError(f"CredDelete failed: {ctypes.get_last_error()}")
