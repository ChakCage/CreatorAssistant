from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from typing import Any


class SecureCredentialStoreError(RuntimeError):
    pass


class WindowsSecureCredentialStore:
    """Minimal Windows generic-credential store used by Commercial licensing."""

    _test_memory: dict[str, str] = {}

    def __init__(self, namespace: str, allow_test_memory: bool = False) -> None:
        self.prefix = namespace.rstrip("/")
        self.allow_test_memory = allow_test_memory

    def target(self, item_id: str, kind: str) -> str:
        safe = "".join(ch for ch in str(item_id) if ch.isalnum() or ch in "-_.")
        return f"{self.prefix}/{safe}/{kind}"

    def write_json(self, item_id: str, value: dict[str, Any], kind: str) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self._write(self.target(item_id, kind), payload)

    def read_json(self, item_id: str, kind: str) -> dict[str, Any]:
        payload = self._read(self.target(item_id, kind))
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise SecureCredentialStoreError("Credential payload is invalid") from exc
        return value if isinstance(value, dict) else {}

    def delete(self, item_id: str, kind: str) -> None:
        self._delete(self.target(item_id, kind))

    @staticmethod
    def _credential_type():
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]
        return CREDENTIALW

    def _write(self, target: str, secret: str) -> None:
        if self.allow_test_memory:
            self._test_memory[target] = secret
            return
        if os.name != "nt":
            raise SecureCredentialStoreError("Windows Credential Manager is unavailable")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_type = self._credential_type()
        raw = secret.encode("utf-16-le")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = credential_type(
            0, 1, target, "Creator Assistant Commercial license", wintypes.FILETIME(),
            len(raw), blob, 2, 0, None, None, "CreatorAssistant",
        )
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(credential_type), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        if not advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise SecureCredentialStoreError(f"CredWrite failed: {ctypes.get_last_error()}")

    def _read(self, target: str) -> str:
        if self.allow_test_memory:
            return self._test_memory.get(target, "")
        if os.name != "nt":
            return ""
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        credential_type = self._credential_type()
        pointer = ctypes.POINTER(credential_type)()
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(credential_type))]
        advapi32.CredReadW.restype = wintypes.BOOL
        if not advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return ""
            raise SecureCredentialStoreError(f"CredRead failed: {ctypes.get_last_error()}")
        try:
            raw = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            advapi32.CredFree(pointer)

    def _delete(self, target: str) -> None:
        if self.allow_test_memory:
            self._test_memory.pop(target, None)
            return
        if os.name != "nt":
            return
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        if not advapi32.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise SecureCredentialStoreError(f"CredDelete failed: {ctypes.get_last_error()}")
