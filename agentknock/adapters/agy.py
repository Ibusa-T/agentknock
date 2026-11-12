"""
agentknock.adapters.agy — Antigravity CLI (agy.exe) Delivery Adapter & Liveness Probe.

特徴:
- Windows カーネル API (AttachConsole + WriteConsoleInputW) を用いて非フォーカスで CONIN$ へ注入。
- 【A1 準拠】: wake(target_id) は target_id (conversation_id UUID) のみを受け取り、text 引数は完全廃止。
- 【B3 準拠】: i18n 定数ナッジカタログ (WAKE_NUDGE_CATALOG) は本アダプタ内にカプセル化（ローカル専権）。
- 【R-M1 準拠】: consumes_marker_on_wake = False (agy.exe が自力で dequeue するため Watcher はマーカーを消さない)。
- 診断/テスト用 seam (_find_pid, _inject) を保持し、既存テスト互換性を維持。
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Final, Mapping, Optional

_CURRENT_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _CURRENT_DIR.parent.parent
for _p in (str(_TOOLS_ROOT), str(_CURRENT_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import _win_console
except ImportError:
    try:
        import pyximport

        pyximport.install(language_level=3, inplace=True)
        import _win_console  # type: ignore[no-redef]
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
        "de": "Antworten Sie auf Deutsch.",
    }
)

WAKE_NUDGE_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {loc: f"{WAKE_BODY_CATALOG[loc]} {OUTPUT_DIRECTIVE_CATALOG[loc]}" for loc in SUPPORTED_LOCALES}
)


def _normalize_locale(candidate: Optional[str]) -> Optional[str]:
    if not isinstance(candidate, str):
        return None
    normalized = candidate.strip().lower()
    return normalized if normalized in WAKE_NUDGE_CATALOG else None


def resolve_wake_prompt(
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> str:
    """ローカル設定から CONIN$ 注入用ナッジ定数文字列を決定論的に解決する純関数 (R2 準拠)。"""
    env_wake = _normalize_locale(os.environ.get(ENV_CONINJECT_WAKE_LOCALE))
    env_common = _normalize_locale(os.environ.get(ENV_CONINJECT_LOCALE))
    w_loc = env_wake or env_common or _normalize_locale(wake_locale) or DEFAULT_LOCALE

    env_out = _normalize_locale(os.environ.get(ENV_CONINJECT_OUTPUT_LOCALE))
    o_loc = env_out or env_common or _normalize_locale(output_locale) or w_loc

    if w_loc == o_loc:
        return WAKE_NUDGE_CATALOG[w_loc]
    return f"{WAKE_BODY_CATALOG[w_loc]} {OUTPUT_DIRECTIVE_CATALOG[o_loc]}"


# ============================================================================
# AgyLockLivenessProbe (A2 実装)
# ============================================================================
class AgyLockLivenessProbe(LivenessProbe):
    """presence/<conversation_id>.lock の実体と PID 保持プロセスを確認するプローブ。"""

    def __init__(self, presence_dir: Optional[Path] = None) -> None:
        self.presence_dir = presence_dir or DEFAULT_PRESENCE_DIR

    def is_alive(self, target_id: str) -> bool:
        if not isinstance(target_id, str) or not _UUID_RE.match(target_id):
            return False
        lock_path = self.presence_dir / f"{target_id}.lock"
        if not lock_path.is_file():
            return False
        if _win_console is None:
            return True
        try:
            pid = _win_console.find_pid_by_lock_path(str(lock_path))
            return bool(pid and pid > 0)
        except Exception:
            return False


# ============================================================================
# AgyDeliveryAdapter (A1 / B3 / R-M1 実装)
# ============================================================================
class AgyDeliveryAdapter(DeliveryAdapter):
    """
    Antigravity CLI (agy.exe) 専用 DeliveryAdapter。
    CONIN$ 注入と Restart Manager PID 逆引きを担う。
    """

    def __init__(
        self,
        *,
        presence_dir: Optional[Path] = None,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
        send_enter: bool = True,
        submit_delay_sec: float = 0.08,
    ) -> None:
        self.presence_dir = presence_dir or DEFAULT_PRESENCE_DIR
        self.wake_locale = wake_locale
        self.output_locale = output_locale
        self.send_enter = send_enter
        self.submit_delay_sec = submit_delay_sec

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

    def _find_pid(self, conversation_id: str) -> Optional[int]:
        """presence/<id>.lock -> PID (L0). 診断/テスト用 monkeypatch seam."""
        if not isinstance(conversation_id, str) or not _UUID_RE.match(conversation_id):
            return None
        lock_path = self.presence_dir / f"{conversation_id}.lock"
        if not lock_path.is_file() or _win_console is None:
            return None
        return _win_console.find_pid_by_lock_path(str(lock_path))

    def _inject(self, pid: int, text: str) -> bool:
        """nogil CONIN$ injection (L0). 診断/テスト用 monkeypatch seam."""
        if _win_console is None:
            return False
        return bool(
            _win_console.inject_console_input(
                pid,
                text,
                self.send_enter,
                self.submit_delay_sec,
            )
        )

    def wake(self, target_id: str) -> bool:
        """
        【A1 型レベル R2 保証】:
        引数は target_id のみ。text 引数は存在しない。
        注入する wake_text はアダプタ内のローカル定数カタログからのみ解決される。
        """
        pid = self._find_pid(target_id)
        if not pid:
            return False

        wake_text = resolve_wake_prompt(
            wake_locale=self.wake_locale,
            output_locale=self.output_locale,
        )
        return self._inject(pid, wake_text)
