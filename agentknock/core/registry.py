# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock core.registry — Opt-in Discovery Registry (A3 / R1 Invariant).

Implements AliasRegistry:
- FileSystemAliasRegistry: Filesystem-backed opt-in self-advertisement registry.
- InMemoryAliasRegistry: Fast dictionary-backed registry for testing and memory-only agents.
- R1 (Rendezvous Secrecy): `list_active()` filters by (valid ∧ unexpired ∧ probe.is_alive)
  and NEVER surfaces `target_id` / `conversation_id`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Final, Optional
import uuid

from agentknock._contract import (
    AGY_BASE_SUBDIR,
    ALIAS_PATTERN,
    REGISTRY_DIRNAME,
    UUID_REGEX,
    AliasRegistry,
    Capabilities,
    LivenessProbe,
    Recipient,
    RegistryEntry,
)

_ALIAS_RE: Final[re.Pattern[str]] = re.compile(ALIAS_PATTERN)
_UUID_RE: Final[re.Pattern[str]] = re.compile(UUID_REGEX, re.IGNORECASE)

DEFAULT_TTL_SEC: Final[float] = 86400.0  # 24 hours
DEFAULT_AGY_REGISTRY_DIR: Final[Path] = (
    Path.home() / AGY_BASE_SUBDIR / REGISTRY_DIRNAME
)

DEFAULT_CAPABILITIES: Final[Capabilities] = {
    "surface_custom_json": False,
    "honor_sender_locale": False,
    "injected_input_trust_label": "USER_EXPLICIT",
}


def is_valid_alias(alias: object) -> bool:
    """
    Validate that alias matches ALIAS_PATTERN (^[a-zA-Z0-9_-]{1,64}$)
    AND is NOT a raw UUID (prevents raw conversation_id leakage as an alias).
    """
    if not isinstance(alias, str):
        return False
    if not _ALIAS_RE.match(alias):
        return False
    if _UUID_RE.match(alias):
        return False
    return True


def _parse_iso_utc(ttl_str: object) -> Optional[datetime]:
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


class FileSystemAliasRegistry(AliasRegistry):
    """Filesystem-backed registry under `~/.gemini/antigravity-cli/registry/<alias>.json`."""

    def __init__(
        self,
        registry_dir: Optional[Path] = None,
        *,
        default_ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> None:
        self.registry_dir = registry_dir or DEFAULT_AGY_REGISTRY_DIR
        self.default_ttl_sec = default_ttl_sec

    def register(
        self,
        alias: str,
        target_id: str,
        *,
        role: str = "",
        capabilities: Optional[Capabilities] = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> None:
        if not is_valid_alias(alias):
            raise ValueError(
                f"Invalid alias (must match {ALIAS_PATTERN} and not be a UUID): {alias!r}"
            )
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("target_id must be a non-empty string")
        if not isinstance(role, str):
            role = str(role)

        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")

        now = datetime.now(timezone.utc)
        expiry_dt = now + timedelta(seconds=ttl_sec)
        expiry_str = expiry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        caps = capabilities if capabilities is not None else dict(DEFAULT_CAPABILITIES)

        entry: RegistryEntry = {
            "alias": alias,
            "conversation_id": target_id,
            "role": role.strip(),
            "capabilities": caps,  # type: ignore[typeddict-item]
            "ttl": expiry_str,
        }

        self.registry_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.registry_dir / f"{alias}.json"
        tmp_path = self.registry_dir / f".{alias}.{uuid.uuid4().hex}.tmp"

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

    def unregister(self, alias: str) -> bool:
        if not is_valid_alias(alias):
            return False
        entry_path = self.registry_dir / f"{alias}.json"
        try:
            entry_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _load_entry(self, entry_path: Path, now: datetime) -> Optional[RegistryEntry]:
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
        conv_id = data.get("conversation_id")
        role = data.get("role", "")
        capabilities = data.get("capabilities", {})
        ttl_str = data.get("ttl")

        if alias != expected_alias or not is_valid_alias(alias):
            return None
        if not isinstance(conv_id, str) or not conv_id.strip():
            return None

        expiry = _parse_iso_utc(ttl_str)
        if expiry is None or expiry <= now:
            return None

        return {
            "alias": alias,
            "conversation_id": conv_id,
            "role": str(role),
            "capabilities": capabilities,  # type: ignore[typeddict-item]
            "ttl": str(ttl_str),
        }

    def resolve(
        self,
        alias: str,
        *,
        probe: Optional[LivenessProbe] = None,
    ) -> Optional[str]:
        if not is_valid_alias(alias):
            return None
        entry_path = self.registry_dir / f"{alias}.json"
        if not entry_path.is_file():
            return None

        now = datetime.now(timezone.utc)
        entry = self._load_entry(entry_path, now)
        if entry is None:
            return None

        target_id = entry["conversation_id"]
        if probe is not None and not probe.is_alive(target_id):
            return None

        return target_id

    def list_active(
        self,
        *,
        probe: Optional[LivenessProbe] = None,
    ) -> list[Recipient]:
        if not self.registry_dir.is_dir():
            return []

        now = datetime.now(timezone.utc)
        recipients: list[Recipient] = []

        for entry_path in sorted(self.registry_dir.glob("*.json")):
            if entry_path.name.startswith("."):
                continue
            entry = self._load_entry(entry_path, now)
            if entry is None:
                continue

            target_id = entry["conversation_id"]
            if probe is not None and not probe.is_alive(target_id):
                continue

            recipients.append(
                {
                    "alias": entry["alias"],
                    "role": entry["role"],
                    "capabilities": dict(entry["capabilities"]),  # type: ignore[typeddict-item]
                }
            )

        return recipients


class InMemoryAliasRegistry(AliasRegistry):
    """Thread-safe in-memory alias registry for testing and standalone usage."""

    def __init__(self, *, default_ttl_sec: float = DEFAULT_TTL_SEC) -> None:
        self.default_ttl_sec = default_ttl_sec
        self._entries: dict[str, tuple[str, str, Capabilities, float]] = {}  # alias -> (target_id, role, caps, expiry_ts)
        self._lock = threading.Lock()

    def register(
        self,
        alias: str,
        target_id: str,
        *,
        role: str = "",
        capabilities: Optional[Capabilities] = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> None:
        if not is_valid_alias(alias):
            raise ValueError(
                f"Invalid alias (must match {ALIAS_PATTERN} and not be a UUID): {alias!r}"
            )
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("target_id must be a non-empty string")
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")

        now = datetime.now(timezone.utc).timestamp()
        caps = capabilities if capabilities is not None else dict(DEFAULT_CAPABILITIES)

        with self._lock:
            self._entries[alias] = (target_id, role, caps, now + ttl_sec)

    def unregister(self, alias: str) -> bool:
        with self._lock:
            return self._entries.pop(alias, None) is not None

    def resolve(
        self,
        alias: str,
        *,
        probe: Optional[LivenessProbe] = None,
    ) -> Optional[str]:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            entry = self._entries.get(alias)
            if entry is None:
                return None
            target_id, _, _, expiry_ts = entry
            if expiry_ts <= now:
                return None

        if probe is not None and not probe.is_alive(target_id):
            return None

        return target_id

    def list_active(
        self,
        *,
        probe: Optional[LivenessProbe] = None,
    ) -> list[Recipient]:
        now = datetime.now(timezone.utc).timestamp()
        recipients: list[Recipient] = []

        with self._lock:
            items = list(self._entries.items())

        for alias, (target_id, role, caps, expiry_ts) in items:
            if expiry_ts <= now:
                continue
            if probe is not None and not probe.is_alive(target_id):
                continue
            recipients.append(
                {
                    "alias": alias,
                    "role": role,
                    "capabilities": dict(caps),
                }
            )

        return sorted(recipients, key=lambda r: r["alias"])
