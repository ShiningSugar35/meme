from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from ..config import PROJECT_ROOT


_TEMP_ROOT = PROJECT_ROOT / "data" / ".browser_session_tmp"
_CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600


@dataclass(frozen=True, slots=True)
class ChromiumProfile:
    browser: str
    name: str
    path: Path
    user_data_root: Path

    @property
    def label(self) -> str:
        return f"{self.browser}:{self.name}"


@dataclass(frozen=True, slots=True)
class BrowserCredentialSnapshot:
    profile: str | None
    values: Mapping[str, str]
    source: str
    unsupported_app_bound: int = 0
    errors: tuple[str, ...] = ()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _safe_error(prefix: str, exc: BaseException) -> str:
    return f"{prefix}:{type(exc).__name__}"


def _dpapi_decrypt(payload: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("windows_dpapi_required")
    buffer = ctypes.create_string_buffer(payload)
    in_blob = _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("dpapi_decrypt_failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _profile_roots() -> tuple[tuple[str, Path], ...]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    return (
        ("chrome", local / "Google" / "Chrome" / "User Data"),
        ("edge", local / "Microsoft" / "Edge" / "User Data"),
    )


def chromium_profiles() -> tuple[ChromiumProfile, ...]:
    profiles: list[ChromiumProfile] = []
    for browser, root in _profile_roots():
        if not root.is_dir():
            continue
        candidates = [root / "Default", *sorted(root.glob("Profile *"))]
        for profile in candidates:
            if profile.is_dir():
                profiles.append(ChromiumProfile(browser, profile.name, profile, root))
    return tuple(profiles)


def _master_key(profile: ChromiumProfile) -> bytes:
    state = json.loads((profile.user_data_root / "Local State").read_text(encoding="utf-8"))
    encoded = str((state.get("os_crypt") or {}).get("encrypted_key") or "")
    if not encoded:
        raise ValueError("missing_os_crypt_key")
    wrapped = base64.b64decode(encoded)
    if wrapped.startswith(b"DPAPI"):
        wrapped = wrapped[5:]
    return _dpapi_decrypt(wrapped)


def _decrypt_cookie_value(encrypted: bytes, key: bytes, host_key: str) -> tuple[str | None, bool]:
    if not encrypted:
        return "", False
    if encrypted.startswith(b"v20"):
        # Chromium App-Bound Encryption deliberately binds decryption to the
        # browser application. Do not bypass it by code injection/elevation.
        return None, True
    if encrypted.startswith((b"v10", b"v11")):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = encrypted[3:15]
        ciphertext_and_tag = encrypted[15:]
        plain = AESGCM(key).decrypt(nonce, ciphertext_and_tag, None)
        digest = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plain.startswith(digest):
            plain = plain[len(digest):]
        return plain.decode("utf-8"), False
    return _dpapi_decrypt(encrypted).decode("utf-8"), False


def _new_temp_dir() -> Path:
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    target = _TEMP_ROOT / uuid.uuid4().hex
    target.mkdir(parents=False, exist_ok=False)
    return target


def _cleanup_temp(target: Path) -> None:
    try:
        shutil.rmtree(target, ignore_errors=True)
    finally:
        try:
            if _TEMP_ROOT.exists() and not any(_TEMP_ROOT.iterdir()):
                _TEMP_ROOT.rmdir()
        except OSError:
            pass


def _copy_sqlite_family(source: Path, target_dir: Path) -> Path:
    target = target_dir / source.name
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sibling = source.with_name(source.name + suffix)
        if sibling.exists():
            try:
                shutil.copy2(sibling, target_dir / sibling.name)
            except OSError:
                pass
    return target


def _domain_matches(host_key: str, domain: str) -> bool:
    wanted = domain.strip().lower().lstrip(".")
    host = host_key.strip().lower().lstrip(".")
    return bool(wanted) and (host == wanted or host.endswith("." + wanted))


def read_cookie_header(domain: str, *, profiles: Sequence[ChromiumProfile] | None = None) -> BrowserCredentialSnapshot:
    """Read a Chromium cookie header in-memory without persisting cookie values.

    Chrome/Edge legacy DPAPI and AES-GCM v10/v11 cookies are supported. v20
    App-Bound cookies are reported as unavailable instead of attempting to
    weaken Chromium's application binding.
    """
    errors: list[str] = []
    unsupported = 0
    now_chrome_us = int((time.time() + _CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000)
    for profile in tuple(chromium_profiles() if profiles is None else profiles):
        cookie_db = profile.path / "Network" / "Cookies"
        if not cookie_db.exists():
            continue
        temp = _new_temp_dir()
        try:
            copied = _copy_sqlite_family(cookie_db, temp)
            key = _master_key(profile)
            connection = sqlite3.connect(str(copied))
            try:
                rows = connection.execute(
                    "SELECT host_key,name,path,expires_utc,encrypted_value,value FROM cookies"
                ).fetchall()
            finally:
                connection.close()
            cookies: list[tuple[int, str, str]] = []
            local_unsupported = 0
            for host_key, name, path, expires_utc, encrypted_value, plain_value in rows:
                if not _domain_matches(str(host_key or ""), domain):
                    continue
                expiry = int(expires_utc or 0)
                if expiry > 0 and expiry <= now_chrome_us:
                    continue
                value = str(plain_value or "")
                if not value:
                    try:
                        decrypted, app_bound = _decrypt_cookie_value(bytes(encrypted_value or b""), key, str(host_key or ""))
                    except Exception as exc:
                        errors.append(_safe_error(profile.label, exc))
                        continue
                    if app_bound:
                        local_unsupported += 1
                        continue
                    value = decrypted or ""
                if name and value:
                    cookies.append((len(str(path or "/")), str(name), value))
            unsupported += local_unsupported
            if cookies:
                cookies.sort(key=lambda item: (-item[0], item[1]))
                header = "; ".join(f"{name}={value}" for _path_len, name, value in cookies)
                return BrowserCredentialSnapshot(
                    profile=profile.label,
                    values={"Cookie": header},
                    source="chromium_cookie",
                    unsupported_app_bound=unsupported,
                    errors=tuple(errors),
                )
        except Exception as exc:
            errors.append(_safe_error(profile.label, exc))
        finally:
            _cleanup_temp(temp)
    return BrowserCredentialSnapshot(
        profile=None,
        values={},
        source="chromium_cookie",
        unsupported_app_bound=unsupported,
        errors=tuple(errors),
    )


def _copy_leveldb(source: Path, target_dir: Path) -> Path:
    target = target_dir / "leveldb"
    target.mkdir(parents=True, exist_ok=True)
    for file in source.iterdir():
        if not file.is_file() or file.name.upper() == "LOCK":
            continue
        try:
            shutil.copy2(file, target / file.name)
        except OSError:
            continue
    return target


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16-le"):
            try:
                return value.decode(encoding).lstrip("\x00\x01")
            except UnicodeDecodeError:
                continue
    return str(value or "")


def _storage_origin_matches(storage_key: Any, origin: str) -> bool:
    text = str(storage_key or "").lower()
    parsed = urlparse(origin)
    host = str(parsed.hostname or "").lower()
    return bool(host) and host in text


def read_local_storage(
    origin: str,
    keys: Iterable[str],
    *,
    profiles: Sequence[ChromiumProfile] | None = None,
) -> BrowserCredentialSnapshot:
    """Read explicitly allowlisted Chromium localStorage keys from a local profile.

    This is intentionally key-scoped: callers cannot enumerate or dump a user's
    browser storage. Values stay in process memory and exception messages never
    include profile data.
    """
    wanted = {str(key) for key in keys if str(key)}
    if not wanted:
        return BrowserCredentialSnapshot(None, {}, "chromium_local_storage")
    errors: list[str] = []
    for profile in tuple(chromium_profiles() if profiles is None else profiles):
        source = profile.path / "Local Storage" / "leveldb"
        if not source.is_dir():
            continue
        temp = _new_temp_dir()
        try:
            copied = _copy_leveldb(source, temp)
            from ccl_chromium_reader import ccl_chromium_localstorage

            found: dict[str, str] = {}
            with ccl_chromium_localstorage.LocalStoreDb(copied) as local_storage:
                for storage_key in local_storage.iter_storage_keys():
                    if not _storage_origin_matches(storage_key, origin):
                        continue
                    for record in local_storage.iter_records_for_storage_key(storage_key):
                        key = _string_value(getattr(record, "script_key", ""))
                        if key not in wanted:
                            continue
                        value = _string_value(getattr(record, "value", ""))
                        if value:
                            found[key] = value
            if found:
                return BrowserCredentialSnapshot(
                    profile=profile.label,
                    values=found,
                    source="chromium_local_storage",
                    errors=tuple(errors),
                )
        except Exception as exc:
            errors.append(_safe_error(profile.label, exc))
        finally:
            _cleanup_temp(temp)
    return BrowserCredentialSnapshot(None, {}, "chromium_local_storage", errors=tuple(errors))
