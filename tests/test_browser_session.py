from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.app.services.browser_session import (
    _cleanup_temp,
    _decrypt_cookie_value,
    _domain_matches,
    _new_temp_dir,
    chromium_profiles,
    read_cookie_header,
    read_local_storage,
)


def test_domain_match_is_exact_or_subdomain_only() -> None:
    assert _domain_matches(".985monitor.xyz", "985monitor.xyz")
    assert _domain_matches("www.985monitor.xyz", "985monitor.xyz")
    assert not _domain_matches("evil985monitor.xyz", "985monitor.xyz")
    assert not _domain_matches("985monitor.xyz.evil.test", "985monitor.xyz")


def test_chromium_v10_cookie_decrypts_without_persisting_secret() -> None:
    key = os.urandom(32)
    nonce = os.urandom(12)
    host = ".985monitor.xyz"
    value = "fixture-cookie-value"
    import hashlib

    plaintext = hashlib.sha256(host.encode("utf-8")).digest() + value.encode("utf-8")
    encrypted = b"v10" + nonce + AESGCM(key).encrypt(nonce, plaintext, None)
    decrypted, app_bound = _decrypt_cookie_value(encrypted, key, host)
    assert app_bound is False
    assert decrypted == value


def test_chromium_v20_cookie_fails_closed_without_bypass() -> None:
    decrypted, app_bound = _decrypt_cookie_value(b"v20" + b"opaque", b"x" * 32, ".example.test")
    assert decrypted is None
    assert app_bound is True


def test_browser_temp_copy_root_stays_inside_project_and_is_cleaned() -> None:
    target = _new_temp_dir()
    try:
        assert target.is_dir()
        assert str(target).lower().startswith(str(target.parents[2]).lower())
        assert "meme" in {part.lower() for part in target.parts}
        (target / "probe.txt").write_text("x", encoding="utf-8")
    finally:
        _cleanup_temp(target)
    assert not target.exists()


def test_browser_readers_fail_closed_with_no_profiles() -> None:
    cookie = read_cookie_header("985monitor.xyz", profiles=())
    storage = read_local_storage("https://985monitor.xyz", ("xMonitorWalletToken",), profiles=())
    assert cookie.values == {}
    assert storage.values == {}
    assert cookie.profile is None
    assert storage.profile is None


def test_profile_inventory_never_contains_credentials() -> None:
    for profile in chromium_profiles():
        rendered = repr(profile)
        assert "cookie" not in rendered.lower()
        assert "token" not in rendered.lower()
