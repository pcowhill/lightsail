"""Configuration parsing and the Origin policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from lightsail_demo.config import ConfigError, load_settings, normalize_origin

PRODUCTION_ENV = {
    "HOST": "127.0.0.1",
    "PORT": "8101",
    "SERVE_STATIC": "0",
    "ALLOWED_ORIGINS": "https://lightsail-demo.cowhill.dev",
}


def test_defaults_are_loopback_8080_with_static_and_development_mode():
    s = load_settings({})
    assert (s.host, s.port, s.serve_static) == ("127.0.0.1", 8080, True)
    assert s.development_mode
    assert s.allowed_origins is None


def test_production_contract_environment():
    s = load_settings(PRODUCTION_ENV)
    assert (s.host, s.port, s.serve_static) == ("127.0.0.1", 8101, False)
    assert not s.development_mode
    assert s.allowed_origins == ("https://lightsail-demo.cowhill.dev",)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("HOST", "0.0.0.0"),
        ("HOST", "::"),
        ("HOST", ""),
        ("HOST", "localhost"),
        ("HOST", "example.com"),
        ("PORT", "0"),
        ("PORT", "70000"),
        ("PORT", "eighty"),
        ("SERVE_STATIC", "maybe"),
        ("ALLOWED_ORIGINS", ""),
        ("ALLOWED_ORIGINS", "lightsail-demo.cowhill.dev"),
        ("ALLOWED_ORIGINS", "https://lightsail-demo.cowhill.dev/"),
        ("ALLOWED_ORIGINS", "https://user@lightsail-demo.cowhill.dev"),
        ("ALLOWED_ORIGINS", "ftp://x"),
        ("LOG_LEVEL", "LOUD"),
        ("WS_MAX_MESSAGE_BYTES", "10"),
        ("WS_MAX_CONNECTIONS", "0"),
        ("WS_RATE_LIMIT_PER_SECOND", "nan"),
        ("WS_RATE_LIMIT_BURST", "-1"),
        ("WS_SEND_QUEUE_LIMIT", "0"),
        ("WS_HEARTBEAT_SECONDS", "-5"),
    ],
)
def test_invalid_values_fail_loudly(key: str, value: str):
    env = dict(PRODUCTION_ENV)
    env[key] = value
    with pytest.raises(ConfigError):
        load_settings(env)


def test_wildcard_host_is_never_accepted_even_in_development():
    with pytest.raises(ConfigError, match="wildcard"):
        load_settings({"HOST": "0.0.0.0"})


def test_ipv6_loopback_host_and_bracketed_origin():
    s = load_settings({"HOST": "::1", "ALLOWED_ORIGINS": "http://[::1]:8080"})
    assert s.host == "::1"
    assert s.allowed_origins == ("http://[::1]:8080",)
    assert s.origin_allowed("http://[::1]:8080")


def test_multiple_origins_are_normalized_and_deduplicated():
    s = load_settings(
        {
            "ALLOWED_ORIGINS": "HTTPS://Lightsail-Demo.cowhill.dev:443, https://lightsail-demo.cowhill.dev https://other.example:8443"
        }
    )
    assert s.allowed_origins == ("https://lightsail-demo.cowhill.dev", "https://other.example:8443")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com", "https://example.com"),
        ("https://example.com:443", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("http://Example.com:8080", "http://example.com:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_normalize_origin(raw: str, expected: str):
    assert normalize_origin(raw) == expected


def test_production_origin_policy_is_exact():
    s = load_settings(PRODUCTION_ENV)
    assert s.origin_allowed("https://lightsail-demo.cowhill.dev")
    assert s.origin_allowed("https://LIGHTSAIL-DEMO.cowhill.dev:443")
    assert not s.origin_allowed("http://lightsail-demo.cowhill.dev")  # scheme matters
    assert not s.origin_allowed("https://lightsail-demo.cowhill.dev.evil.example")
    assert not s.origin_allowed("https://evil.example")
    assert not s.origin_allowed("http://127.0.0.1:8101")  # loopback is not allowed in production
    assert not s.origin_allowed("null")
    assert not s.origin_allowed("")
    assert not s.origin_allowed(None)


def test_development_origin_policy_is_loopback_http_only():
    s = load_settings({})
    assert s.origin_allowed("http://localhost:8080")
    assert s.origin_allowed("http://127.0.0.1:8080")
    assert s.origin_allowed("http://127.0.0.1:5173")  # any loopback port (frontend dev servers)
    assert s.origin_allowed("http://[::1]:8080")
    assert not s.origin_allowed("https://localhost:8080")
    assert not s.origin_allowed("http://localhost.evil.example")
    assert not s.origin_allowed("http://192.168.1.10:8080")
    assert not s.origin_allowed("http://lightsail-demo.cowhill.dev")
    assert not s.origin_allowed(None)


def test_app_dir_is_resolved_and_public_dir_derived(tmp_path: Path):
    link = tmp_path / "current"
    real = tmp_path / "release"
    real.mkdir()
    link.symlink_to(real)
    s = load_settings({}, app_dir=link)
    assert s.app_dir == real.resolve()
    assert s.public_dir == real.resolve() / "public"
    assert s.revision_file == real.resolve() / "REVISION"


def test_heartbeat_zero_disables():
    assert load_settings({"WS_HEARTBEAT_SECONDS": "0"}).ws_heartbeat_seconds is None
