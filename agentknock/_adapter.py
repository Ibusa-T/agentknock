# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock Internal Delivery Adapter (L1/L0) — PRIVATE MODULE

DESIGN-0001 D0/D1 契約 + v0.3.1 TOCTOU 封鎖:
- `_contract._AgyAdapter` プロトコル (`_find_pid`, `_find_process_identity`, `_inject`, `wake`) を実装する。
- `resolve_alias` は L2 純関数 (`_resolve_alias_from_registry`) に分離されたため本クラスには持たない。
- 受信側常駐ウォッチャ (orochi) および診断 CLI (`doctor` / `--undelivered-file`) 専用。
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Final, Optional, Sequence

_ADAPTER_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _ADAPTER_DIR.parent
for _p in (str(_TOOLS_ROOT), str(_ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import _win_console as _wc_cand

    if getattr(_wc_cand, "_BACKEND", None) == "cython" or str(
        getattr(_wc_cand, "__file__", "")
    ).lower().endswith((".pyd", ".so")):
        _win_console = _wc_cand
    else:
        from agentknock.adapters import _win_console_ctypes as _win_console  # type: ignore[no-redef]
except ImportError:
    try:
        from agentknock.adapters import _win_console_ctypes as _win_console  # type: ignore[no-redef]
    except Exception:
        _win_console = None  # type: ignore[assignment]

from console_injector import (
    AGY_BASE_DIR,
    BRAIN_DIR,
    PRESENCE_DIR,
    REGISTRY_DIR,
    UUID_REGEX,
    list_recipients,
    resolve_output_locale,
    resolve_wake_locale,
    resolve_wake_prompt,
)

DEFAULT_ALLOWED_IMAGES: Final[tuple[str, ...]] = ("agy.exe",)


def _find_agy_process_by_conversation_id(
    conversation_id: str,
    *,
    presence_dir: Optional[Path] = None,
    allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
) -> Optional[tuple[int, int]]:
    """
    [L1/L0 非公開]
    presence/<conversation_id>.lock を掴んでいる適格プロセスの `(pid, start_time_u64)` を
    `_win_console.find_process_by_lock_path` 経由で逆引きする。
    """
    if not isinstance(conversation_id, str) or not UUID_REGEX.match(conversation_id):
        raise ValueError(f"不正な会話ID形式です: {conversation_id}")

    if _win_console is None:
        return None

    target_presence = presence_dir or PRESENCE_DIR
    lock_path = target_presence / f"{conversation_id}.lock"
    if not lock_path.exists():
        return None

    find_proc = getattr(_win_console, "find_process_by_lock_path", None)
    if not callable(find_proc):
        return None

    identity = find_proc(str(lock_path), allowed_images=allowed_images)
    if (
        not identity
        or not isinstance(identity, tuple)
        or len(identity) != 2
        or not isinstance(identity[0], int)
        or isinstance(identity[0], bool)
        or not isinstance(identity[1], int)
        or isinstance(identity[1], bool)
        or identity[0] <= 0
        or identity[1] <= 0
    ):
        return None
    return (int(identity[0]), int(identity[1]))


def _find_agy_pid_by_conversation_id(
    conversation_id: str,
    *,
    presence_dir: Optional[Path] = None,
    allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
) -> Optional[int]:
    """
    [L1/L0 非公開]
    presence/<conversation_id>.lock を掴んでいる agy.exe の PID を逆引きする互換ラッパー。
    """
    identity = _find_agy_process_by_conversation_id(
        conversation_id,
        presence_dir=presence_dir,
        allowed_images=allowed_images,
    )
    return identity[0] if identity else None


def _inject_console_input(
    target_pid: int,
    text: Optional[str] = None,
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
    *,
    expected_start_time: Optional[int] = None,
    allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> bool:
    """
    [L1/L0 非公開]
    指定した PID (agy.exe) の CONIN$ に HANDLE ピン留め + ProcessStartTime 検証付きで
    ローカル定数カタログ由来の起床ナッジと Enter キーを直接書き込む。
    Fail-Closed: expected_start_time が None または <= 0 の場合は即 False を返す。
    """
    if (
        _win_console is None
        or not isinstance(target_pid, int)
        or isinstance(target_pid, bool)
        or target_pid <= 0
        or expected_start_time is None
        or not isinstance(expected_start_time, int)
        or isinstance(expected_start_time, bool)
        or expected_start_time <= 0
    ):
        return False

    resolved_text = (
        text
        if text is not None
        else resolve_wake_prompt(wake_locale=wake_locale, output_locale=output_locale)
    )
    return bool(
        _win_console.inject_console_input(
            target_pid,
            resolved_text,
            send_enter,
            submit_delay_sec,
            expected_start_time=expected_start_time,
            allowed_images=allowed_images,
        )
    )


class _AgyAdapter:
    """
    `_contract._AgyAdapter` 準拠の非公開アダプタ実装 (`_find_pid`, `_find_process_identity`, `_inject`, `wake`)。
    ※ D0 Amendment #1: `resolve_alias` は L2 `_resolve_alias_from_registry` に分離済みのため持たない。
    """

    def __init__(
        self,
        *,
        base_dir: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
        presence_dir: Optional[Path] = None,
        registry_dir: Optional[Path] = None,
        allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
    ) -> None:
        self.base_dir = base_dir or AGY_BASE_DIR
        self.brain_dir = brain_dir or BRAIN_DIR
        self.presence_dir = presence_dir or PRESENCE_DIR
        self.registry_dir = registry_dir or REGISTRY_DIR
        self.allowed_images: Optional[tuple[str, ...]] = (
            tuple(allowed_images) if allowed_images is not None else None
        )
        self.last_wake_detail: Optional[dict[str, Any]] = None

    def _find_process_identity(
        self, conversation_id: str
    ) -> Optional[tuple[int, int]]:
        """presence/<id>.lock -> (pid, start_time_u64) (L0). Monkeypatch seam for unit tests."""
        return _find_agy_process_by_conversation_id(
            conversation_id,
            presence_dir=self.presence_dir,
            allowed_images=self.allowed_images,
        )

    def _find_pid(self, conversation_id: str) -> Optional[int]:
        """presence/<id>.lock -> PID (L0). Compatibility wrapper."""
        identity = self._find_process_identity(conversation_id)
        return identity[0] if identity else None

    def _inject(
        self,
        pid: int,
        text: str,
        *,
        expected_start_time: Optional[int] = None,
        send_enter: bool = True,
        submit_delay_sec: float = 0.08,
    ) -> bool:
        """
        HANDLE-pinned & ProcessStartTime-verified CONIN$ injection (L0).
        Fail-Closed: returns False if expected_start_time is None or <= 0.
        """
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or expected_start_time is None
            or not isinstance(expected_start_time, int)
            or isinstance(expected_start_time, bool)
            or expected_start_time <= 0
        ):
            self.last_wake_detail = {
                "reason": "invalid_pid_or_start_time",
                "win32_error": 0,
                "pid": pid,
                "verified_start_time": expected_start_time,
            }
            return False

        if _win_console is None:
            self.last_wake_detail = {
                "reason": "win_console_unavailable",
                "win32_error": 0,
                "pid": pid,
                "verified_start_time": expected_start_time,
            }
            return False

        inject_verified = getattr(_win_console, "inject_console_input_verified", None)
        if callable(inject_verified):
            ok, reason, win32_err = inject_verified(
                pid,
                expected_start_time,
                text,
                send_enter,
                submit_delay_sec,
                allowed_images=self.allowed_images,
            )
            self.last_wake_detail = {
                "reason": str(reason),
                "win32_error": int(win32_err),
                "pid": pid,
                "verified_start_time": expected_start_time,
                "backend": getattr(_win_console, "_BACKEND", "unknown"),
            }
            return bool(ok)

        return _inject_console_input(
            target_pid=pid,
            text=text,
            send_enter=send_enter,
            submit_delay_sec=submit_delay_sec,
            expected_start_time=expected_start_time,
            allowed_images=self.allowed_images,
        )

    def wake(
        self,
        conversation_id: str,
        *,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
        send_enter: bool = True,
        submit_delay_sec: float = 0.08,
    ) -> bool:
        """
        _find_process_identity -> _inject(local wake constant, expected_start_time=start_time).
        R2: `wake_text` は常にローカル定数カタログから解決され、msg_data には一切依存しない。
        """
        self.last_wake_detail = None
        identity = self._find_process_identity(conversation_id)
        if not identity:
            self.last_wake_detail = {
                "reason": "lock_holder_not_found_or_ambiguous",
                "win32_error": 0,
            }
            return False

        pid, start_time = identity
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(start_time, int)
            or isinstance(start_time, bool)
            or start_time <= 0
        ):
            self.last_wake_detail = {
                "reason": "invalid_pid_or_start_time",
                "win32_error": 0,
                "pid": pid,
                "verified_start_time": start_time,
            }
            return False

        resolved_wake = resolve_wake_locale(wake_locale)
        resolved_output = resolve_output_locale(
            output_locale, fallback_locale=resolved_wake
        )
        wake_text = resolve_wake_prompt(
            wake_locale=resolved_wake,
            output_locale=resolved_output,
        )
        ok = self._inject(
            pid,
            wake_text,
            expected_start_time=start_time,
            send_enter=send_enter,
            submit_delay_sec=submit_delay_sec,
        )
        if self.last_wake_detail is None:
            self.last_wake_detail = {
                "reason": "ok" if ok else "inject_failed",
                "win32_error": 0,
                "pid": pid,
                "verified_start_time": start_time,
            }
        return bool(ok)


def _run_doctor(adapter: Optional["_AgyAdapter"] = None) -> dict[str, Any]:
    """診断 CLI (`doctor`) 用ヘルスチェック。環境・ロック・台帳の健全性を検証する。"""
    adp = adapter or _default_adapter
    lock_files = (
        list(adp.presence_dir.glob("*.lock")) if adp.presence_dir.exists() else []
    )
    alive_locks = 0
    for lf in lock_files:
        cid = lf.stem
        if UUID_REGEX.match(cid) and adp._find_process_identity(cid):
            alive_locks += 1

    reg_files = (
        list(adp.registry_dir.glob("*.json")) if adp.registry_dir.exists() else []
    )
    active_aliases = sorted(
        r["alias"]
        for r in list_recipients(
            registry_dir=adp.registry_dir,
            presence_dir=adp.presence_dir,
        )
    )
    active_set = set(active_aliases)
    inactive_aliases = sorted(
        rf.stem
        for rf in reg_files
        if not rf.name.startswith(".") and rf.stem not in active_set
    )

    backend_name = (
        getattr(_win_console, "_BACKEND", "unknown")
        if _win_console is not None
        else None
    )
    toctou_hardened = bool(
        _win_console is not None
        and callable(getattr(_win_console, "inject_console_input_verified", None))
        and callable(getattr(_win_console, "find_process_by_lock_path", None))
    )

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "native_extension_loaded": _win_console is not None,
        "win_console_backend": backend_name,
        "toctou_hardened": toctou_hardened,
        "directories": {
            "base_dir": adp.base_dir.exists(),
            "brain_dir": adp.brain_dir.exists(),
            "presence_dir": adp.presence_dir.exists(),
            "registry_dir": adp.registry_dir.exists(),
        },
        "presence_locks": {
            "total_files": len(lock_files),
            "alive_processes": alive_locks,
        },
        "registry": {
            "total_entries": len(
                [rf for rf in reg_files if not rf.name.startswith(".")]
            ),
            "active_aliases": active_aliases,
            "inactive_aliases": inactive_aliases,
        },
        "effective_locales": {
            "wake_locale": resolve_wake_locale(),
            "output_locale": resolve_output_locale(),
        },
    }


_default_adapter = _AgyAdapter()
