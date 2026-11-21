"""Per-user Windows Credential Manager storage; no plaintext secret file."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    return api


def read_secret(target: str) -> str:
    api = _api()
    pointer = ctypes.POINTER(Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return ""
        raise OSError(error, "Windows Credential Manager read failed")
    try:
        item = pointer.contents
        return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def write_secret(target: str, secret: str) -> None:
    raw = secret.encode("utf-8")
    if len(raw) > 2560:
        raise ValueError("credential exceeds Windows storage limit")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    item = Credential(Type=1, TargetName=target, UserName="polymarket-private-key",
                      CredentialBlobSize=len(raw), CredentialBlob=buffer, Persist=2)
    if not _api().CredWriteW(ctypes.byref(item), 0):
        raise OSError(ctypes.get_last_error(), "Windows Credential Manager write failed")


def delete_secret(target: str) -> None:
    if not _api().CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
        raise OSError(ctypes.get_last_error(), "Windows Credential Manager delete failed")
