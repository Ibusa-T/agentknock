# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock D3 — Opt-in Discovery Registry & R1-Compliant Recipient Listing (L2).

Security Invariants:
- R1 (Rendezvous Secrecy): No indiscriminate scan of `presence/*.lock`. Only
  agents that explicitly opt in via `register_self()` appear in `registry/<alias>.json`.
  `list_recipients()` filters by (unexpired TTL AND live presence lock) and NEVER
  exposes `conversation_id` in its return value.
- R2 (Call-Graph Isolation): This module is pure L2 filesystem I/O and MUST NOT
  import `_adapter` or `_win_console`, keeping `send() -> _resolve_alias_from_registry()`
  completely disconnected from CONIN$ / Win32 APIs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Callable, Optional
import uuid

try:
    from ._contract import (
        AGY_BASE_SUBDIR,
        ALIAS_PATTERN,
        REGISTRY_DIRNAME,
        UUID_REGEX,
        Capabilities,
        Recipient,
        RegistryEntry,
    )
except ImportError:
    from _contract import (  # type: ignore[no-redef]
        AGY_BASE_SUBDIR,
        ALIAS_PATTERN,
        REGISTRY_DIRNAME,
        UUID_REGEX,
        Capabilities,
        Recipient,
        RegistryEntry,
    )

_ALIAS_RE = re.compile(ALIAS_PATTERN)
_UUID_RE = re.compile(UUID_REGEX, re.IGNORECASE)

DEFAULT_AGY_BASE_DIR: Path = Path.home() / AGY_BASE_SUBDIR
DEFAULT_REGISTRY_DIR: Path = DEFAULT_AGY_BASE_DIR / REGISTRY_DIRNAME
DEFAULT_PRESENCE_DIR: Path = DEFAULT_AGY_BASE_DIR / "presence"
DEFAULT_TTL_SECONDS: int = 3600

DEFAULT_AGY_CAPABILITIES: Capabilities = {
    "surface_custom_json": False,
    "honor_sender_locale": False,
    "injected_input_trust_label": "USER_EXPLICIT",
}


def is_valid_alias(alias: object) -> bool:
    """
    Return True iff `alias` matches ALIAS_PATTERN (^[a-zA-Z0-9_-]{1,64}$)
    AND is NOT a raw UUID (prevents raw conversation_id leakage as an alias).
    """
    if not isinstance(alias, str):
        return False
    if not _ALIAS_RE.match(alias):
        return False
    if _UUID_RE.match(alias):
        return False
    return True


def is_valid_uuid(value: object) -> bool:
    """Return True iff `value` is a canonical lowercase UUID string."""
    return isinstance(value, str) and bool(re.match(UUID_REGEX, value))


def _parse_iso_utc(ttl_str: object) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware datetime."""
    if not isinstance(ttl_str, str) or not ttl_str.strip():
        return None
    normalized = ttl_str.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_valid_entry(
    entry_path: Path,
    *,
    now: datetime,
) -> Optional[RegistryEntry]:
    """
    Read and validate a single `registry/<alias>.json` file.
    Returns None if unreadable, malformed, alias mismatch, or TTL-expired.
    """
    expected_alias = entry_path.stem
    if not is_valid_alias(expected_alias):
        return None
    try:
        raw = entry_path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    alias = data.get("alias")
    conversation_id = data.get("conversation_id")
    role = data.get("role")
    capabilities = data.get("capabilities")
    ttl_str = data.get("ttl")

    if alias != expected_alias or not is_valid_alias(alias):
        return None
    if not is_valid_uuid(conversation_id):
        return None
    if not isinstance(role, str) or not role.strip():
        return None
    if not isinstance(capabilities, dict):
        return None

    expiry = _parse_iso_utc(ttl_str)
    if expiry is None or expiry <= now:
        return None

    return {
        "alias": alias,
        "conversation_id": conversation_id,
        "role": role,
        "capabilities": capabilities,  # type: ignore[typeddict-item]
        "ttl": ttl_str,
    }


def _resolve_alias_from_registry(
    alias: str,
    *,
    registry_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    L2 pure-file alias resolution: `registry/<alias>.json` -> `conversation_id`.
    Returns None if alias is invalid, unknown, malformed, or TTL-expired.
    Contains zero Win32 / console code (safe for `send()` static call graph).
    """
    if not is_valid_alias(alias):
        return None
    target_dir = registry_dir or DEFAULT_REGISTRY_DIR
    entry_path = target_dir / f"{alias}.json"
    if not entry_path.is_file():
        return None

    current_time = now or datetime.now(timezone.utc)
    entry = _load_valid_entry(entry_path, now=current_time)
    if entry is None:
        return None
    return entry["conversation_id"]


def list_recipients(
    *,
    registry_dir: Optional[Path] = None,
    presence_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    is_lock_alive: Optional[Callable[[str, Path], bool]] = None,
) -> list[Recipient]:
    """
    L3 Public Discovery API (R1-compliant):
    Return only opt-in registry entries whose TTL is unexpired AND whose
    `presence/<conversation_id>.lock` file exists (and passes `is_lock_alive`
    if provided). Never includes `conversation_id` in returned dicts.
    """
    target_registry = registry_dir or DEFAULT_REGISTRY_DIR
    target_presence = presence_dir or DEFAULT_PRESENCE_DIR
    if not target_registry.is_dir():
        return []

    current_time = now or datetime.now(timezone.utc)
    recipients: list[Recipient] = []

    for entry_path in sorted(target_registry.glob("*.json")):
        if entry_path.name.startswith("."):
            continue
        entry = _load_valid_entry(entry_path, now=current_time)
        if entry is None:
            continue

        conv_id = entry["conversation_id"]
        lock_path = target_presence / f"{conv_id}.lock"
        if not lock_path.is_file():
            continue
        if is_lock_alive is not None and not is_lock_alive(conv_id, lock_path):
            continue

        recipients.append(
            {
                "alias": entry["alias"],
                "role": entry["role"],
                "capabilities": dict(entry["capabilities"]),  # type: ignore[typeddict-item]
            }
        )

    return recipients


def register_self(
    alias: str,
    conversation_id: str,
    role: str,
    *,
    capabilities: Optional[Capabilities] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ttl_iso: Optional[str] = None,
    registry_dir: Optional[Path] = None,
    presence_dir: Optional[Path] = None,
    require_live_lock: bool = True,
    now: Optional[datetime] = None,
) -> Path:
    """
    Internal opt-in self-advertisement writer (NOT exported in public `__all__`).
    Atomically writes `registry/<alias>.json` via a temp file + `os.replace`.
    """
    if not is_valid_alias(alias):
        raise ValueError(f"Invalid alias (must match {ALIAS_PATTERN} and not be a UUID): {alias!r}")
    if not is_valid_uuid(conversation_id):
        raise ValueError(f"Invalid conversation_id UUID: {conversation_id!r}")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")

    target_presence = presence_dir or DEFAULT_PRESENCE_DIR
    if require_live_lock:
        lock_path = target_presence / f"{conversation_id}.lock"
        if not lock_path.is_file():
            raise RuntimeError(
                f"Cannot advertise inactive session: presence lock not found for {conversation_id}"
            )

    current_time = now or datetime.now(timezone.utc)
    if ttl_iso is not None:
        parsed_ttl = _parse_iso_utc(ttl_iso)
        if parsed_ttl is None or parsed_ttl <= current_time:
            raise ValueError(f"Invalid or already-expired ttl_iso: {ttl_iso!r}")
        expiry_str = parsed_ttl.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expiry_dt = current_time + timedelta(seconds=ttl_seconds)
        expiry_str = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    target_registry = registry_dir or DEFAULT_REGISTRY_DIR
    target_registry.mkdir(parents=True, exist_ok=True)

    entry: RegistryEntry = {
        "alias": alias,
        "conversation_id": conversation_id,
        "role": role.strip(),
        "capabilities": capabilities if capabilities is not None else dict(DEFAULT_AGY_CAPABILITIES),  # type: ignore[typeddict-item]
        "ttl": expiry_str,
    }

    final_path = target_registry / f"{alias}.json"
    tmp_path = target_registry / f".{alias}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return final_path


def unregister_self(
    alias: str,
    *,
    conversation_id: Optional[str] = None,
    registry_dir: Optional[Path] = None,
) -> bool:
    """
    Internal opt-in self-advertisement removal (NOT exported in public `__all__`).
    If `conversation_id` is specified, verifies ownership before unlinking.
    """
    if not is_valid_alias(alias):
        raise ValueError(f"Invalid alias: {alias!r}")
    target_registry = registry_dir or DEFAULT_REGISTRY_DIR
    entry_path = target_registry / f"{alias}.json"
    if not entry_path.is_file():
        return False

    if conversation_id is not None:
        try:
            data = json.loads(entry_path.read_text(encoding="utf-8-sig"))
            if data.get("conversation_id") != conversation_id:
                raise PermissionError("conversation_id mismatch: cannot unregister another session's alias")
        except PermissionError:
            raise
        except Exception:
            pass

    try:
        entry_path.unlink()
        return True
    except FileNotFoundError:
        return False
