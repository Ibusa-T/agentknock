# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock.adapters.callback — CallbackDeliveryAdapter (Core Default Adapter).

特徴:
- Python コールバック関数を叩いてメッセージ着信を通知する汎用アダプタ（標準ライブラリのみ・外部依存ゼロ）。
- 【A1 準拠】: wake(target_id) は target_id のみを受け取り、text 引数は一切持たない。
- consumes_marker_on_wake = True: コールバック実行で同期的に処理完了するため、Watcher は即時マーカー消費可能。
"""
from __future__ import annotations

from typing import Callable, Optional

from agentknock._contract import Capabilities, DeliveryAdapter


class CallbackDeliveryAdapter(DeliveryAdapter):
    """
    受信コールバックを駆動する汎用アダプタ。
    CI・単体テスト・埋め込み利用における既定の DeliveryAdapter。
    """

    def __init__(
        self,
        callback: Optional[Callable[[str], bool]] = None,
        *,
        surface_custom_json: bool = True,
        honor_sender_locale: bool = True,
    ) -> None:
        self._callback = callback
        self._capabilities: Capabilities = {
            "surface_custom_json": surface_custom_json,
            "honor_sender_locale": honor_sender_locale,
            "injected_input_trust_label": "CALLBACK",
        }

    @property
    def capabilities(self) -> Capabilities:
        return dict(self._capabilities)  # type: ignore[return-value]

    @property
    def consumes_marker_on_wake(self) -> bool:
        """同期コールバック完了と同時にマーカー消費を完了してよい。"""
        return True

    def wake(self, target_id: str) -> bool:
        """
        【A1 型レベル R2 保証】:
        target_id のみを受け取り、自由文字列 text は受け付けない。
        """
        if self._callback is None:
            return True
        try:
            res = self._callback(target_id)
            return True if res is None else bool(res)
        except Exception:
            return False
