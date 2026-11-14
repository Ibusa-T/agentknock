"""
agentknock _contract — Frozen Protocols & Types (v0.3.0 / BR1-SPEC).

This module defines the frozen integration contracts for agentknock v0.3:
- Pure types and protocols (zero behavior, zero OS-specific imports).
- 4 Extension Points: DeliveryAdapter, LivenessProbe, AliasRegistry, Notifier.
- DeadLetterStore for atomic quarantine of failed messages.
- Backward-compatible type aliases for v0.2 migration.
"""
from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Callable,
    Final,
    Literal,
    Optional,
    Protocol,
    TypedDict,
    runtime_checkable,
)

# ---- 識別子正規表現契約 (凍結) -----------------------------------------------
ALIAS_PATTERN: Final[str] = r"^[a-zA-Z0-9_-]{1,64}$"
UUID_REGEX: Final[str] = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ---- on-disk layout --------------------------------------------------------
AGY_BASE_SUBDIR: Final[str] = ".gemini/antigravity-cli"
REGISTRY_DIRNAME: Final[str] = "registry"          # ~/.gemini/antigravity-cli/registry/<alias>.json
DEAD_LETTER_DIRNAME: Final[str] = "dead_letter"    # .system_generated/messages/dead_letter/<id>.json


# ---- Capability Contract (凍結) ---------------------------------------------
class Capabilities(TypedDict, total=False):
    """Per-target adapter capability contract."""
    surface_custom_json: bool
    honor_sender_locale: bool
    injected_input_trust_label: str          # "USER_EXPLICIT" | "SYSTEM" | "ATTRIBUTED_METADATA"


class Recipient(TypedDict):
    """Public shape returned by list_recipients(). No target_id (UUID hidden, R1)."""
    alias: str
    role: str
    capabilities: Capabilities

"""
メッセージの送信元を見る
"""
class MessagePayload(TypedDict):
    """Frozen Message JSON schema."""
    id: str
    recipient: str
    sender: str
    priority: Literal["MESSAGE_PRIORITY_HIGH", "MESSAGE_PRIORITY_NORMAL", "MESSAGE_PRIORITY_LOW"]
    timestamp: str
    renderDetails: dict[str, str]            # {"messageTitle": "[sender-supplied] <title>"}
    content: str

"""
エージェント自身の情報
セッション・エイリアス
信頼性の証明
通信の有効期限
"""
class RegistryEntry(TypedDict):
    """registry/<alias>.json — opt-in self-advertisement record."""
    alias: str
    conversation_id: str                     # internal only; NEVER surfaced to callers
    role: str
    capabilities: Capabilities
    ttl: str                                 # ISO-8601 expiry (UTC)


# ---- 拡張点1: DeliveryAdapter (A1・B3 反映) ----------------------------------
"""
各プラットフォームの共通インターフェース
現状はAntigravity対応のみ
"""
@runtime_checkable
class DeliveryAdapter(Protocol):
    """
    【A1 型レベル R2 保証】:
    wake() は target_id のみを受け取り、自由文字列 text 引数を一切持たない。
    起床ナッジ定数カタログの保持・解決は各アダプタ内部の専権責務とする (B3)。
    """
    
    """
    通信を試みてきた相手の信頼性の担保を取るためのプロパティ
    """
    @property
    def capabilities(self) -> Capabilities: ...

    @property
    def consumes_marker_on_wake(self) -> bool:
        """
        True: wake() 内で同期的に処理完了するため Watcher がマーカーを即時消費してよい (例: CallbackDeliveryAdapter)。
        False: 外部プロセス (agy.exe 等) が起床後に自分でマーカーを読むため、Watcher はマーカーを unlink しない (R-M1)。
        """
        ...

    def wake(self, target_id: str) -> bool:
        """target_id を起床させる。送信者由来データを引数に取る経路は存在しない。"""
        ...


# ---- 拡張点2: LivenessProbe (A2 反映) ----------------------------------------
@runtime_checkable
class LivenessProbe(Protocol):
    """R1 の生存確認を抽象化する拡張点。"""
    def is_alive(self, target_id: str) -> bool: ...


# ---- 拡張点3: AliasRegistry (A3 反映) ----------------------------------------
@runtime_checkable
class AliasRegistry(Protocol):
    """Mailbox から独立した opt-in 自己広告台帳インターフェース。"""
    def register(
        self,
        alias: str,
        target_id: str,
        *,
        role: str = "",
        capabilities: Optional[Capabilities] = None,
        ttl_sec: float = 86400.0,
    ) -> None: ...

    def unregister(self, alias: str) -> bool:
        """Kill switch: 台帳から即時削除し、以降の discovery / 送信対象から外す。"""
        ...

    def resolve(self, alias: str, *, probe: Optional[LivenessProbe] = None) -> Optional[str]:
        """
        (registry well-formed ∧ TTL未失効 ∧ (probe is None or probe.is_alive(target_id)))
        を満たす場合のみ target_id を返す。
        """
        ...

    def list_active(self, *, probe: Optional[LivenessProbe] = None) -> list[Recipient]:
        """生存かつ未失効の Recipient 一覧を返す (target_id は含めない)。"""
        ...


# ---- 拡張点4: Notifier (B6 反映) ---------------------------------------------
@runtime_checkable
class Notifier(Protocol):
    """新着マーカー検知インターフェース (WatchdogNotifier / PollingNotifier)。"""
    def subscribe(self, directory: Path, on_created: Callable[[Path], None]) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...


# ---- DeadLetterStore (B2 反映) -----------------------------------------------
@runtime_checkable
class DeadLetterStore(Protocol):
    """検証失敗・リトライ上限到達メッセージをアトミック隔離するストア。"""
    def quarantine(
        self,
        marker_path: Path,
        *,
        message_id: str,
        recipient_id: str,
        sender_id: str,
        attempts: int,
        reason: str,
    ) -> tuple[Path, str]:
        """(dead_letter_path, tier_label) を返す。"""
        ...


# ---- 後方互換性プロトコル (v0.2 互換用) ----------------------------------------

"""
通信相手にWatcherのフラグとなるファイルを置くためのインターフェース
リストから通信相手を探す
通信相手へファイルを置きに行く
"""
@runtime_checkable
class PublicApi(Protocol):
    def list_recipients(self) -> list[Recipient]: ...
    def send(self, recipient: str, content: str, *, sender: str) -> str: ...
    

"""
injectでファイルが置かれたことを通知する
どこのプロセスから来たか特定する find_pid
二つを集約しているwake
"""
@runtime_checkable
class _AgyAdapter(Protocol):
    def _inject(
        self,
        pid: int,
        text: str,
        *,
        expected_start_time: Optional[int] = None,
    ) -> bool: ...
    def _find_process_identity(
        self, conversation_id: str
    ) -> Optional[tuple[int, int]]: ...
    def _find_pid(self, conversation_id: str) -> Optional[int]: ...
    def wake(
        self,
        conversation_id: str,
        *,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
    ) -> bool: ...

"""
injectが通知していないファイル(マーカー)の有無を監視する
"""
@runtime_checkable
class _Watcher(Protocol):
    def on_undelivered(self, undelivered_path: str) -> bool: ...
