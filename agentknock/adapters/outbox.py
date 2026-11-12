"""
agentknock.adapters.outbox — Outbox Delivery Adapter & Claude Liveness Probe (BR1 仕様).

特徴:
- Gemini (agy.exe) から Claude Code セッションへの軽量返信ブリッジ。
- 送信側は共有 outbox ディレクトリへ署名付き JSON を投函するだけで完了 (CONIN$ 注入なし・R2 非抵触)。
- 受信側 (Claude Code 監視プロセス) は本アダプタ経由でイベントを受け取り、SendMessage / 画面通知へ橋渡しする。
- 【A1 準拠】: wake(target_id) は target_id のみを受け取る。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from agentknock._contract import Capabilities, DeliveryAdapter, LivenessProbe

CLAUDE_SESSIONS_DIR: Path = Path.home() / ".claude" / "sessions"


class ClaudeSessionLivenessProbe(LivenessProbe):
    """~/.claude/sessions/<PID>.json を走査して Claude Code セッションの生存を確認するプローブ。"""

    def __init__(self, sessions_dir: Optional[Path] = None) -> None:
        self.sessions_dir = sessions_dir or CLAUDE_SESSIONS_DIR

    def is_alive(self, target_id: str) -> bool:
        if not self.sessions_dir.is_dir():
            return False
        for session_file in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                # sessionId またはセッション名 (name) の両方に対応
                if data.get("sessionId") == target_id or data.get("name") == target_id:
                    return data.get("status") in ("idle", "running")
            except Exception:
                continue
        return False


class OutboxDeliveryAdapter(DeliveryAdapter):
    """
    共有 Outbox 方式の DeliveryAdapter (BR1).
    Gemini -> Claude 返信において、受信側 watcher へ通知フラグを立てる / ハンドラを呼ぶ。
    """

    def __init__(
        self,
        outbox_dir: Optional[Path] = None,
        *,
        on_wake_signal: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.outbox_dir = outbox_dir
        self._on_wake_signal = on_wake_signal

    @property
    def capabilities(self) -> Capabilities:
        return {
            "surface_custom_json": True,
            "honor_sender_locale": False,
            "injected_input_trust_label": "OUTBOX_SIGNED_PAYLOAD",
        }

    @property
    def consumes_marker_on_wake(self) -> bool:
        """受信側 (Claude 側) が自前で dequeue するため Watcher は消さない。"""
        return False

    def wake(self, target_id: str) -> bool:
        """
        【A1 型レベル R2 保証】:
        target_id のみを受け取る。
        通知シグナルハンドラまたは outbox 状態更新を実行する。
        """
        if self._on_wake_signal is not None:
            try:
                return bool(self._on_wake_signal(target_id))
            except Exception:
                return False

        if self.outbox_dir is not None:
            try:
                self.outbox_dir.mkdir(parents=True, exist_ok=True)
                wake_flag = self.outbox_dir / f".wake_{target_id}"
                wake_flag.touch()
                return True
            except OSError:
                return False

        return True
