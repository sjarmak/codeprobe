"""Environment validators used by ``codeprobe doctor``."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)
PRIVATE_CA_FILE_ENV_KEYS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
PRIVATE_CA_DIR_ENV_KEYS: tuple[str, ...] = ("SSL_CERT_DIR",)

_PROXY_URL_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_SUPPORTED_PROXY_SCHEMES: tuple[str, ...] = (
    "http",
    "https",
    "socks4",
    "socks4a",
    "socks5",
    "socks5h",
)
_DOCTOR_AGENT_OFFLINE_BACKENDS: dict[str, tuple[str, ...]] = {
    "claude": ("anthropic",),
    "codex": ("openai_compat",),
}


def env_value(key: str) -> str:
    return os.environ.get(key, "").strip()


def env_has_value(key: str) -> bool:
    return bool(env_value(key))


def _proxy_url_is_valid(raw: str) -> bool:
    if raw != raw.strip() or _contains_control(raw):
        return False
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in _SUPPORTED_PROXY_SCHEMES:
        return False
    if parsed.hostname is None:
        return False
    if any(ch.isspace() for ch in parsed.hostname):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    return True


def _no_proxy_is_valid(raw: str) -> bool:
    parts = raw.split(",")
    return all(
        part
        and part == part.strip()
        and not any(char.isspace() for char in part)
        and not _contains_control(part)
        for part in parts
    )


def _contains_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def proxy_variables_status() -> tuple[bool, str]:
    configured_count = 0
    invalid: list[str] = []
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key, "")
        if not value:
            continue
        configured_count += 1
        if key in _PROXY_URL_ENV_KEYS:
            valid = _proxy_url_is_valid(value)
        else:
            valid = _no_proxy_is_valid(value)
        if not valid:
            invalid.append(key)
    if invalid:
        return False, f"invalid: {', '.join(invalid)}"
    detail = f"{configured_count} configured" if configured_count else "not configured"
    return True, detail


def _path_has_kind(raw: str, *, directory: bool) -> bool:
    try:
        path = Path(raw)
        if directory:
            return path.is_dir() and os.access(path, os.R_OK | os.X_OK)
        if not path.is_file():
            return False
        with path.open("rb"):
            return True
    except (OSError, ValueError):
        return False


def private_ca_files_status(private_ca_paths: tuple[str, ...]) -> tuple[bool, str]:
    unreadable: list[str] = []
    configured_count = 0
    for raw in private_ca_paths:
        value = raw.strip()
        if not value:
            continue
        configured_count += 1
        if not _path_has_kind(value, directory=False):
            unreadable.append("--private-ca")
    for key in PRIVATE_CA_FILE_ENV_KEYS:
        raw = env_value(key)
        if raw:
            configured_count += 1
        if raw and not _path_has_kind(raw, directory=False):
            unreadable.append(key)
    for key in PRIVATE_CA_DIR_ENV_KEYS:
        raw = env_value(key)
        if raw:
            configured_count += 1
        if raw and not _path_has_kind(raw, directory=True):
            unreadable.append(key)
    if unreadable:
        return False, f"unreadable: {', '.join(dict.fromkeys(unreadable))}"
    detail = f"{configured_count} configured" if configured_count else "not configured"
    return True, detail


def offline_backends_for_agent(selected_agent: str | None) -> tuple[str, ...]:
    if selected_agent is None:
        return ()
    return _DOCTOR_AGENT_OFFLINE_BACKENDS.get(selected_agent, ())
