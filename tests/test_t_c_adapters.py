"""
agentknock T-C Acceptance Criteria Test Suite (A1, B1, BR1).
"""
import inspect
from pathlib import Path
import sys
import uuid
import pytest

# Ensure agentknock directory is in sys.path
_AGENTKNOCK_DIR = Path(__file__).resolve().parent.parent
if str(_AGENTKNOCK_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTKNOCK_DIR))

from agentknock.adapters.callback import CallbackDeliveryAdapter
from agentknock.adapters.agy import AgyDeliveryAdapter
from agentknock.adapters.outbox import OutboxDeliveryAdapter, ClaudeSessionLivenessProbe
from agentknock.testing import assert_send_cannot_reach
from agentknock.core.mailbox import Mailbox
from agentknock.core.registry import FileSystemAliasRegistry


def test_a1_wake_signature_has_no_text_argument():
    """
    【A1 受入条件】:
    全 DeliveryAdapter の wake() シグネチャが target_id のみを受け取り、
    text 引数を一切持たないことを inspect で検証する。
    """
    adapters = [
        CallbackDeliveryAdapter(),
        AgyDeliveryAdapter(),
        OutboxDeliveryAdapter(),
    ]
    for adapter in adapters:
        sig = inspect.signature(adapter.wake)
        param_names = list(sig.parameters.keys())
        assert "target_id" in param_names, f"{adapter} missing target_id parameter"
        assert "text" not in param_names, f"A1 Violation: {adapter}.wake has forbidden 'text' argument"
        assert len(param_names) == 1, f"A1 Violation: {adapter}.wake has extra arguments: {param_names}"


def test_b1_assert_send_cannot_reach_passes():
    """
    【B1 受入条件】:
    testing.py の assert_send_cannot_reach() を console_injector.py および
    agentknock/__init__.py に実行して PASS することを確認する。
    """
    root_dir = Path(__file__).parent.parent
    ci_file = root_dir / "console_injector.py"
    init_file = root_dir / "agentknock" / "__init__.py"

    assert_send_cannot_reach(ci_file, entry_func_name="send")
    assert_send_cannot_reach(init_file, entry_func_name="send")


def test_br1_gemini_to_claude_outbox_roundtrip(tmp_path):
    """
    【BR1 受入条件】:
    Gemini -> Claude 返信用 OutboxDeliveryAdapter 往復テスト。
    送信側が mailbox.post() で投函し、受信側が OutboxDeliveryAdapter 経由で着信を捕捉できること。
    """
    outbox_dir = tmp_path / "outbox"
    reg_dir = tmp_path / "registry"

    claude_cid = str(uuid.uuid4())
    claude_alias = "claude-commander"

    # 1. Claude 側が自己登録 (QS1 / A3)
    registry = FileSystemAliasRegistry(registry_dir=reg_dir)
    registry.register(
        claude_alias,
        claude_cid,
        role="Commander",
    )
    resolved_id = registry.resolve(claude_alias)
    assert resolved_id == claude_cid

    # 2. OutboxDeliveryAdapter のセットアップ
    woken_targets = []
    outbox_adapter = OutboxDeliveryAdapter(
        outbox_dir=outbox_dir,
        on_wake_signal=lambda tid: woken_targets.append(tid) or True,
    )

    # 3. Gemini 側が mailbox.post() で投函 (Outbox 共有ディレクトリへ)
    mailbox = Mailbox(root_dir=outbox_dir)
    msg_id, marker_path = mailbox.post(
        recipient_id=resolved_id,
        content="Task report from Gemini #1",
        sender="gemini-oslayer",
        title="Status Report",
    )
    assert msg_id is not None
    assert marker_path.is_file()

    # 4. OutboxDeliveryAdapter の wake(claude_cid) が着信を正常処理すること
    ok = outbox_adapter.wake(claude_cid)
    assert ok is True
    assert woken_targets == [claude_cid]
