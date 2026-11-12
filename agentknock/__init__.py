"""
agentknock Public API Surface (v0.3.0 / BR1-SPEC B5 & QS1).

特徴:
1. DI クラス `AgentKnock`:
   Mailbox / AliasRegistry / LivenessProbe を任意に差し替え可能なコンテナ。
2. トップレベル糖衣関数:
   send, list_recipients, register_self, unregister_self, configure_default (既定インスタンスへ委譲)。
3. 【R2 Namespace 不変条件】:
   本モジュールおよび send() の静的コールグラフから DeliveryAdapter / _win_console / CONIN$ へは
   一切到達不能 (core.mailbox / core.registry / core.probe のみで完結)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from agentknock._contract import (
    AliasRegistry,
    Capabilities,
    DeliveryAdapter,
    LivenessProbe,
    Recipient,
)
from agentknock.core.mailbox import Mailbox
from agentknock.core.probe import FileLockLivenessProbe
from agentknock.core.registry import (
    DEFAULT_TTL_SEC,
    FileSystemAliasRegistry,
    is_valid_alias,
)


class AgentKnock:
    """
    agentknock DI クライアント (B5).
    Mailbox / Registry / LivenessProbe を任意に組み合わせてエージェント間通信を行う。
    """

    def __init__(
        self,
        mailbox: Optional[Mailbox] = None,
        registry: Optional[AliasRegistry] = None,
        probe: Optional[LivenessProbe] = None,
    ) -> None:
        self.mailbox = mailbox or Mailbox()
        self.registry = registry or FileSystemAliasRegistry()
        self.probe = probe or FileLockLivenessProbe()

    def send(self, recipient: str, content: str, *, sender: str) -> str:
        """
        recipient (alias) 宛てに署名付き JSON メッセージをアトミック投函する。
        R2 保証: コールグラフはファイル書き込みのみで完結し、CONIN$ / アダプタには到達しない。
        """
        if not is_valid_alias(recipient):
            raise ValueError(f"Invalid recipient alias: {recipient!r}")
        target_id = self.registry.resolve(recipient, probe=self.probe)
        if target_id is None:
            raise LookupError(f"Recipient alias not found or inactive: {recipient!r}")
        msg_id, _ = self.mailbox.post(target_id, content, sender=sender)
        return msg_id

    def list_recipients(self) -> list[Recipient]:
        """Opt-in かつ生存中・未失効の recipient 一覧を取得する (R1 準拠)。"""
        return self.registry.list_active(probe=self.probe)

    def register_self(
        self,
        alias: str,
        target_id: str,
        *,
        role: str = "",
        capabilities: Optional[Capabilities] = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
    ) -> None:
        """自身のセッションを opt-in 公開台帳に登録する (QS1)。"""
        self.registry.register(
            alias,
            target_id,
            role=role,
            capabilities=capabilities,
            ttl_sec=ttl_sec,
        )

    def unregister_self(self, alias: str) -> bool:
        """Kill switch: 台帳から自身を削除し、以降の受信を即時遮断する。"""
        return self.registry.unregister(alias)


# ============================================================================
# 既定インスタンス ＆ トップレベル糖衣ファサード (B5 確定仕様)
# ============================================================================
_default_client: AgentKnock = AgentKnock()


def send(recipient: str, content: str, *, sender: str) -> str:
    """トップレベル糖衣: 既定 AgentKnock インスタンスへ委譲してメッセージを送信。"""
    return _default_client.send(recipient, content, sender=sender)


def list_recipients() -> list[Recipient]:
    """トップレベル糖衣: 既定 AgentKnock インスタンスへ委譲して有効 recipient 一覧を取得。"""
    return _default_client.list_recipients()


def register_self(
    alias: str,
    target_id: str,
    *,
    role: str = "",
    capabilities: Optional[Capabilities] = None,
    ttl_sec: float = DEFAULT_TTL_SEC,
) -> None:
    """トップレベル糖衣: 既定 AgentKnock インスタンスへ委譲して自身を登録。"""
    _default_client.register_self(
        alias,
        target_id,
        role=role,
        capabilities=capabilities,
        ttl_sec=ttl_sec,
    )


def unregister_self(alias: str) -> bool:
    """トップレベル糖衣: 既定 AgentKnock インスタンスへ委譲して自身を登録解除 (Kill Switch)。"""
    return _default_client.unregister_self(alias)


def configure_default(
    *,
    mailbox: Optional[Mailbox] = None,
    registry: Optional[AliasRegistry] = None,
    probe: Optional[LivenessProbe] = None,
) -> None:
    """既定 AgentKnock インスタンスを再構成する。"""
    global _default_client
    _default_client = AgentKnock(
        mailbox=mailbox,
        registry=registry,
        probe=probe,
    )


__all__ = [
    "AgentKnock",
    "send",
    "list_recipients",
    "register_self",
    "unregister_self",
    "configure_default",
]


def __dir__() -> list[str]:
    """dir(agentknock) に内部アダプタや inject/win_console を露出させない。"""
    return list(__all__)
