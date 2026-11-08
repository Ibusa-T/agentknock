"""
agentknock Internal Delivery Adapter (L1/L0) — PRIVATE MODULE

DESIGN-0001 D0/D1 契約:
- `_contract._AgyAdapter` プロトコル (`_find_pid`, `_inject`, `wake`) を実装する。
- `resolve_alias` は L2 純関数 (`_resolve_alias_from_registry`) に分離されたため本クラスには持たない。
- 受信側常駐ウォッチャ (orochi) および診断 CLI (`doctor` / `--undelivered-file`) 専用。
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Optional

_ADAPTER_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _ADAPTER_DIR.parent
for _p in (str(_TOOLS_ROOT), str(_ADAPTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import _win_console
except ImportError:
    import pyximport

    pyximport.install(language_level=3, inplace=True)
    import _win_console  # type: ignore[no-redef]

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


def _find_agy_pid_by_conversation_id(
    conversation_id: str,
    *,
    presence_dir: Optional[Path] = None,
) -> Optional[int]:
    """
    [L1/L0 非公開]
    presence/<conversation_id>.lock を掴んでいる agy.exe の PID を
    Cython 拡張 (`_win_console.find_pid_by_lock_path`) 経由で逆引きする。
    """
    if not isinstance(conversation_id, str) or not UUID_REGEX.match(conversation_id):
        raise ValueError(f"不正な会話ID形式です: {conversation_id}")

    target_presence = presence_dir or PRESENCE_DIR
    lock_path = target_presence / f"{conversation_id}.lock"
    if not lock_path.exists():
        return None

    return _win_console.find_pid_by_lock_path(str(lock_path))


def _inject_console_input(
    target_pid: int,
    text: Optional[str] = None,
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
    *,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> bool:
    """
    [L1/L0 非公開]
    指定した PID (agy.exe) の CONIN$ に GIL 解放 (`nogil`) 状態で
    ローカル定数カタログ由来の起床ナッジと Enter キーを直接書き込む。
    """
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
        )
    )


class _AgyAdapter:
    """
    `_contract._AgyAdapter` 準拠の非公開アダプタ実装 (`_find_pid`, `_inject`, `wake`)。
    ※ D0 Amendment #1: `resolve_alias` は L2 `_resolve_alias_from_registry` に分離済みのため持たない。
    """

    def __init__(
        self,
        *,
        base_dir: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
        presence_dir: Optional[Path] = None,
        registry_dir: Optional[Path] = None,
    ) -> None:
        self.base_dir = base_dir or AGY_BASE_DIR
        self.brain_dir = brain_dir or BRAIN_DIR
        self.presence_dir = presence_dir or PRESENCE_DIR
        self.registry_dir = registry_dir or REGISTRY_DIR

    def _find_pid(self, conversation_id: str) -> Optional[int]:
        """presence/<id>.lock -> PID (L0). Monkeypatch seam for unit tests."""
        return _find_agy_pid_by_conversation_id(
            conversation_id,
            presence_dir=self.presence_dir,
        )

    def _inject(
        self,
        pid: int,
        text: str,
        *,
        send_enter: bool = True,
        submit_delay_sec: float = 0.08,
    ) -> bool:
        """nogil CONIN$ injection (L0). Monkeypatch seam for unit tests."""
        return _inject_console_input(
            target_pid=pid,
            text=text,
            send_enter=send_enter,
            submit_delay_sec=submit_delay_sec,
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
        _find_pid -> _inject(local wake constant).
        R2: `wake_text` は常にローカル定数カタログから解決され、msg_data には一切依存しない。
        """
        pid = self._find_pid(conversation_id)
        if not pid:
            return False

        resolved_wake = resolve_wake_locale(wake_locale)
        resolved_output = resolve_output_locale(output_locale, fallback_locale=resolved_wake)
        wake_text = resolve_wake_prompt(
            wake_locale=resolved_wake,
            output_locale=resolved_output,
        )
        return self._inject(
            pid,
            wake_text,
            send_enter=send_enter,
            submit_delay_sec=submit_delay_sec,
        )


def _run_doctor(adapter: Optional["_AgyAdapter"] = None) -> dict[str, Any]:
    """診断 CLI (`doctor`) 用ヘルスチェック。環境・ロック・台帳の健全性を検証する。"""
    adp = adapter or _default_adapter
    lock_files = (
        list(adp.presence_dir.glob("*.lock")) if adp.presence_dir.exists() else []
    )
    alive_locks = 0
    for lf in lock_files:
        cid = lf.stem
        if UUID_REGEX.match(cid) and adp._find_pid(cid):
            alive_locks += 1

    reg_files = (
        list(adp.registry_dir.glob("*.json")) if adp.registry_dir.exists() else []
    )
    # 有効 (opt-in + TTL未失効 + presenceロック生存) な alias は list_recipients で判定する
    active_aliases = sorted(
        r["alias"]
        for r in list_recipients(
            registry_dir=adp.registry_dir,
            presence_dir=adp.presence_dir,
        )
    )
    active_set = set(active_aliases)
    inactive_aliases = sorted(
        rf.stem for rf in reg_files if not rf.name.startswith(".") and rf.stem not in active_set
    )

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "native_extension_loaded": _win_console is not None,
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
            "total_entries": len([rf for rf in reg_files if not rf.name.startswith(".")]),
            "active_aliases": active_aliases,
            "inactive_aliases": inactive_aliases,
        },
        "effective_locales": {
            "wake_locale": resolve_wake_locale(),
            "output_locale": resolve_output_locale(),
        },
    }


_default_adapter = _AgyAdapter()
