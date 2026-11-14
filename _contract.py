"""
agentknock D0 — Frozen integration contract (DESIGN-0001).

This module freezes the interface seam that D1-D5 build against. It defines
ONLY types and signatures (no behavior): the public L3 surface, the internal
L1/L0 adapter protocol, the opt-in discovery registry schema, and the on-disk
layout constants. Implementations live elsewhere and MUST conform to these
names and shapes. Owned by the integrator (my-prompt-ee); do not edit without
an ADR update.

Invariants this shape enforces:
- R2 (Wake, Not Command): `send()` MUST NOT be able to reach CONIN$ / PID /
  injection. Only `_AgyAdapter.wake()` (called by the receiver-side watcher)
  touches L0. The public surface has no code path to `_adapter`.
- R1 (rendezvous secrecy): discovery is opt-in only. `list_recipients()` MUST
  return only registry entries that are unexpired AND whose presence lock is
  alive, and MUST NOT expose `conversation_id`.
"""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict, runtime_checkable

# ---- alias / sender format (kept in sync with ALLOWED_SENDERS) --------------
ALIAS_PATTERN: str = r"^[a-zA-Z0-9_-]{1,64}$"
# A raw 36-char UUID also matches ALIAS_PATTERN, so alias validation MUST ALSO
# reject anything matching UUID_REGEX (raw conversation ids may never be used as
# an alias / recipient). Enforced by is_valid_alias() (D1) and the MCP schema.
UUID_REGEX: str = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# ---- on-disk layout --------------------------------------------------------
AGY_BASE_SUBDIR: str = ".gemini/antigravity-cli"
REGISTRY_DIRNAME: str = "registry"          # ~/.gemini/antigravity-cli/registry/<alias>.json
DEAD_LETTER_DIRNAME: str = "dead_letter"    # .system_generated/messages/dead_letter/<id>.json


class Capabilities(TypedDict, total=False):
    """Per-target adapter capability contract (surfaced in docs by D5)."""
    surface_custom_json: bool
    honor_sender_locale: bool
    injected_input_trust_label: str          # "USER_EXPLICIT" | "SYSTEM" | ...


class RegistryEntry(TypedDict):
    """registry/<alias>.json — opt-in self-advertisement record (D3)."""
    alias: str
    conversation_id: str                     # internal only; NEVER surfaced to callers
    role: str
    capabilities: Capabilities
    ttl: str                                 # ISO-8601 expiry (UTC)


class Recipient(TypedDict):
    """Public shape returned by list_recipients(). No conversation_id (UUID hidden)."""
    alias: str
    role: str
    capabilities: Capabilities


# ===========================================================================
# L3 — public surface (the ONLY agent-facing API). D1 implements & re-exports.
# ===========================================================================
@runtime_checkable
class PublicApi(Protocol):
    def send(self, recipient: str, content: str, *, sender: str) -> str:
        """recipient = alias (not a raw UUID). Write a signed JSON message to the
        mailbox and return its message_id. MUST NOT reach CONIN$/PID/inject."""
        ...

    def list_recipients(self) -> list[Recipient]:
        """Opt-in + alive only (R1). MUST NOT include conversation_id."""
        ...


def send(recipient: str, content: str, *, sender: str) -> str:  # pragma: no cover
    """D1: implement in agentknock public L3 and re-export from __init__."""
    raise NotImplementedError("D0 stub: public send() not yet implemented (D1)")


def list_recipients() -> list[Recipient]:  # pragma: no cover
    """D3: implement opt-in registry-backed discovery (R1-compliant)."""
    raise NotImplementedError("D0 stub: list_recipients() not yet implemented (D3)")


# ---------------------------------------------------------------------------
# L2 pure-file alias resolution (NO Win32). D0 amendment #1 (ratified 2026-11-08):
# alias->conversation_id resolution lives HERE, not on _AgyAdapter, so that
# send()'s static import/call graph never reaches _adapter/_win_console (keeps
# the R2 static analysis clean). Both send() (L3) and the watcher (D2) resolve
# aliases through this function; _AgyAdapter does NOT expose resolve_alias.
# ---------------------------------------------------------------------------
def _resolve_alias_from_registry(alias: str) -> Optional[str]:  # pragma: no cover
    """registry/<alias>.json -> conversation_id (pure file read; None if unknown
    or ttl-expired). D3 implements. Contains no Win32 / console code."""
    raise NotImplementedError("D0 stub: _resolve_alias_from_registry() (D3)")


# ===========================================================================
# L1/L0 — internal adapter (NOT exported). D1 moves impl here; D2 calls wake().
# ===========================================================================
@runtime_checkable
class _AgyAdapter(Protocol):
    """Receiver-side agy.exe adapter. Only the watcher and the doctor CLI call it.

    NOTE (D0 amendment #1): alias resolution is NOT a method here — it lives in
    the L2 pure function `_resolve_alias_from_registry` so that send()'s call
    graph stays disconnected from this Win32-touching module (R2)."""

    def _find_process_identity(
        self, conversation_id: str
    ) -> Optional[tuple[int, int]]:
        """presence/<id>.lock -> (pid, start_time_u64) (L0). Monkeypatch seam for unit tests."""
        ...

    def _find_pid(self, conversation_id: str) -> Optional[int]:
        """presence/<id>.lock -> PID (L0). Compatibility wrapper."""
        ...

    def _inject(
        self,
        pid: int,
        text: str,
        *,
        expected_start_time: Optional[int] = None,
    ) -> bool:
        """HANDLE-pinned CONIN$ injection (L0). Monkeypatch seam for unit tests."""
        ...

    def wake(
        self,
        conversation_id: str,
        *,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
    ) -> bool:
        """_find_pid -> _inject(local wake constant). Monkeypatch seam for
        watcher/integration tests. R2: `text` is ALWAYS a local catalog constant
        and is never derived from msg_data."""
        ...


# ===========================================================================
# Receiver-side watcher (D2) — resident; the sole production caller of wake().
# ===========================================================================
@runtime_checkable
class _Watcher(Protocol):
    def on_undelivered(self, undelivered_path: str) -> bool:
        """debounce -> validate_and_load_message -> _AgyAdapter.wake();
        on failure: exponential backoff -> dead_letter/."""
        ...
