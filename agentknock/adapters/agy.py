# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock.adapters.agy — Antigravity CLI (agy.exe) Delivery Adapter & Liveness Probe.

特徴:
- Windows カーネル API (OpenProcess ピン留め + AttachConsole + WriteConsoleInputW) を用いて非フォーカスで CONIN$ へ注入。
- 【A1 準拠】: wake(target_id) は target_id (conversation_id UUID) のみを受け取り、text 引数は完全廃止。
- 【B3 準拠】: i18n 定数ナッジカタログ (WAKE_NUDGE_CATALOG) は本アダプタ内にカプセル化（ローカル専権）。
- 【R-M1 準拠】: consumes_marker_on_wake = False (agy.exe が自力で dequeue するため Watcher はマーカーを消さない)。
- 【v0.3.1 TOCTOU 封鎖】: (pid, start_time) 検証 + Kernel HANDLE ピン留め + 実行イメージ名 allowlist 検証を強制 (fail-closed)。
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional, Sequence

_CURRENT_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _CURRENT_DIR.parent.parent
for _p in (str(_TOOLS_ROOT), str(_CURRENT_DIR.parent)):
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

from agentknock._contract import (
    AGY_BASE_SUBDIR,
    UUID_REGEX,
    Capabilities,
    DeliveryAdapter,
    LivenessProbe,
)

_UUID_RE = re.compile(UUID_REGEX)
DEFAULT_AGY_BASE_DIR: Path = Path.home() / AGY_BASE_SUBDIR
DEFAULT_PRESENCE_DIR: Path = DEFAULT_AGY_BASE_DIR / "presence"
DEFAULT_ALLOWED_IMAGES: Final[tuple[str, ...]] = ("agy.exe",)

# ============================================================================
# i18n 起床ナッジ定数カタログ (B3: アダプタ内包)
# ============================================================================
DEFAULT_LOCALE: Final[str] = "en"
ENV_CONINJECT_LOCALE: Final[str] = "CONINJECT_LOCALE"
ENV_CONINJECT_WAKE_LOCALE: Final[str] = "CONINJECT_WAKE_LOCALE"
ENV_CONINJECT_OUTPUT_LOCALE: Final[str] = "CONINJECT_OUTPUT_LOCALE"

SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("en", "ja", "zh", "ko", "es", "fr", "de")

WAKE_BODY_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "en": "A new message arrived. Review your inbox and begin.",
        "ja": "受信メッセージを確認して作業に着手してください。",
        "zh": "收到新消息，请查看收件箱并开始处理。",
        "ko": "새 메시지가 도착했습니다. 수신함을 확인하고 작업을 시작하세요.",
        "es": "Llegó un nuevo mensaje. Revisa tu bandeja de entrada y comienza.",
        "fr": "Un nouveau message est arrivé. Consultez votre boîte de réception et commencez.",
        "de": "Eine neue Nachricht ist eingegangen. Prüfen Sie Ihren Posteingang und beginnen Sie.",
    }
)

OUTPUT_DIRECTIVE_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "en": "Respond in English.",
        "ja": "日本語で応答してください。",
        "zh": "请用中文回复。",
        "ko": "한국어로 응답해 주세요.",
        "es": "Responde en español.",
        "fr": "Répondez en français.",
        "de": "Bitte antworten Sie auf Deutsch.",
    }
)

WAKE_NUDGE_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        loc: f"{WAKE_BODY_CATALOG[loc]} {OUTPUT_DIRECTIVE_CATALOG[loc]}"
        for loc in SUPPORTED_LOCALES
    }
)


def _normalize_locale(raw: Optional[str]) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    norm = raw.strip().lower().replace("_", "-")
    primary = norm.split("-", 1)[0]
    return primary if primary in SUPPORTED_LOCALES else None


def resolve_wake_prompt(
    *,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> str:
    """ローカル環境変数・設定のみから固定起床プロンプトを解決する（外部メッセージ非依存）。"""
    env_wake = _normalize_locale(os.environ.get(ENV_CONINJECT_WAKE_LOCALE))
    env_common = _normalize_locale(os.environ.get(ENV_CONINJECT_LOCALE))
    w_loc = env_wake or env_common or _normalize_locale(wake_locale) or DEFAULT_LOCALE

    env_out = _normalize_locale(os.environ.get(ENV_CONINJECT_OUTPUT_LOCALE))
    o_loc = env_out or env_common or _normalize_locale(output_locale) or w_loc

    if w_loc == o_loc:
        return WAKE_NUDGE_CATALOG[w_loc]
    return f"{WAKE_BODY_CATALOG[w_loc]} {OUTPUT_DIRECTIVE_CATALOG[o_loc]}"


# ============================================================================
# AgyLockLivenessProbe (A2 実装 / v0.3.1 Fail-Closed 強化)
# ============================================================================
class AgyLockLivenessProbe(LivenessProbe):
    """presence/<conversation_id>.lock の実体と (pid, start_time) 保持プロセスを確認するプローブ。"""

    def __init__(
        self,
        presence_dir: Optional[Path] = None,
        *,
        allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
    ) -> None:
        self.presence_dir = presence_dir or DEFAULT_PRESENCE_DIR
        self.allowed_images: Optional[tuple[str, ...]] = (
            tuple(allowed_images) if allowed_images is not None else None
        )

    def is_alive(self, target_id: str) -> bool:
        if not isinstance(target_id, str) or not _UUID_RE.match(target_id):
            return False
        lock_path = self.presence_dir / f"{target_id}.lock"
        if not lock_path.is_file():
            return False
        if _win_console is None:
            return False
        try:
            find_proc = getattr(_win_console, "find_process_by_lock_path", None)
            if callable(find_proc):
                identity = find_proc(str(lock_path), allowed_images=self.allowed_images)
                return bool(
                    identity
                    and isinstance(identity, tuple)
                    and len(identity) == 2
                    and isinstance(identity[0], int)
                    and not isinstance(identity[0], bool)
                    and isinstance(identity[1], int)
                    and not isinstance(identity[1], bool)
                    and identity[0] > 0
                    and identity[1] > 0
                )
            return False
        except Exception:
            return False


# ============================================================================
# AgyDeliveryAdapter (A1 / B3 / R-M1 / v0.3.1 TOCTOU 封鎖実装)
# ============================================================================
class AgyDeliveryAdapter(DeliveryAdapter):
    """
    Antigravity CLI (agy.exe) 専用 DeliveryAdapter。
    CONIN$ 注入と Restart Manager プロセス (pid, start_time) 逆引きを担う。
    """

    def __init__(
        self,
        *,
        presence_dir: Optional[Path] = None,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
        send_enter: bool = True,
        submit_delay_sec: float = 0.08,
        allowed_images: Optional[Sequence[str]] = DEFAULT_ALLOWED_IMAGES,
    ) -> None:
        self.presence_dir = presence_dir or DEFAULT_PRESENCE_DIR
        self.wake_locale = wake_locale
        self.output_locale = output_locale
        self.send_enter = send_enter
        self.submit_delay_sec = submit_delay_sec
        self.allowed_images: Optional[tuple[str, ...]] = (
            tuple(allowed_images) if allowed_images is not None else None
        )
        self.last_wake_detail: Optional[dict[str, Any]] = None

    @property
    def capabilities(self) -> Capabilities:
        return {
            "surface_custom_json": False,
            "honor_sender_locale": False,
            "injected_input_trust_label": "USER_EXPLICIT",
        }

    @property
    def consumes_marker_on_wake(self) -> bool:
        """【R-M1 準拠】: agy.exe が後から自力でマーカーを読むため Watcher は消さない。"""
        return False

    def _find_process_identity(
        self, conversation_id: str
    ) -> Optional[tuple[int, int]]:
        """presence/<id>.lock -> (pid, start_time_u64) (L0). 検証付き monkeypatch seam."""
        if not isinstance(conversation_id, str) or not _UUID_RE.match(conversation_id):
            return None
        lock_path = self.presence_dir / f"{conversation_id}.lock"
        if not lock_path.is_file() or _win_console is None:
            return None
        find_proc = getattr(_win_console, "find_process_by_lock_path", None)
        if not callable(find_proc):
            return None
        identity = find_proc(str(lock_path), allowed_images=self.allowed_images)
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

    def _find_pid(self, conversation_id: str) -> Optional[int]:
        """presence/<id>.lock -> PID (L0). 互換ラッパー."""
        identity = self._find_process_identity(conversation_id)
        return identity[0] if identity else None

    def _inject(
        self,
        pid: int,
        text: str,
        *,
        expected_start_time: Optional[int] = None,
        send_enter: Optional[bool] = None,
        submit_delay_sec: Optional[float] = None,
    ) -> bool:
        """
        HANDLE ピン留め + ProcessStartTime 検証付き CONIN$ 注入 (L0).
        Fail-Closed: expected_start_time が None または <= 0 の場合は即 False を返す。
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

        eff_enter = self.send_enter if send_enter is None else send_enter
        eff_delay = (
            self.submit_delay_sec if submit_delay_sec is None else submit_delay_sec
        )
        backend_name = getattr(_win_console, "_BACKEND", "unknown")

        inject_verified = getattr(_win_console, "inject_console_input_verified", None)
        if callable(inject_verified):
            ok, reason, win32_err = inject_verified(
                pid,
                expected_start_time,
                text,
                eff_enter,
                eff_delay,
                allowed_images=self.allowed_images,
            )
            self.last_wake_detail = {
                "reason": str(reason),
                "win32_error": int(win32_err),
                "pid": pid,
                "verified_start_time": expected_start_time,
                "backend": backend_name,
            }
            return bool(ok)

        ok = bool(
            _win_console.inject_console_input(
                pid,
                text,
                eff_enter,
                eff_delay,
                expected_start_time=expected_start_time,
                allowed_images=self.allowed_images,
            )
        )
        self.last_wake_detail = {
            "reason": "ok" if ok else "inject_failed",
            "win32_error": 0,
            "pid": pid,
            "verified_start_time": expected_start_time,
            "backend": backend_name,
        }
        return ok

    def wake(self, target_id: str) -> bool:
        """
        【A1 型レベル R2 保証 & v0.3.1 TOCTOU 封鎖】:
        引数は target_id のみ。text 引数は存在しない。
        _find_process_identity -> _inject(pid, wake_text, expected_start_time=start_time) を通じて
        HANDLE ピン留め検証を強制する。
        """
        self.last_wake_detail = None
        if not isinstance(target_id, str) or not _UUID_RE.match(target_id):
            self.last_wake_detail = {
                "reason": "invalid_conversation_id",
                "win32_error": 0,
            }
            return False

        identity = self._find_process_identity(target_id)
        if not identity:
            lock_path = self.presence_dir / f"{target_id}.lock"
            if _win_console is None:
                reason = "win_console_unavailable"
            elif not lock_path.is_file():
                reason = "lock_file_missing"
            else:
                reason = "lock_holder_not_found_or_ambiguous"
            self.last_wake_detail = {
                "reason": reason,
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

        wake_text = resolve_wake_prompt(
            wake_locale=self.wake_locale,
            output_locale=self.output_locale,
        )
        ok = self._inject(pid, wake_text, expected_start_time=start_time)
        if self.last_wake_detail is None:
            self.last_wake_detail = {
                "reason": "ok" if ok else "inject_failed",
                "win32_error": 0,
                "pid": pid,
                "verified_start_time": start_time,
            }
        return bool(ok)
