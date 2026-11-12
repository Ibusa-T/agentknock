"""
agentknock v0.3.0 Facade & Namespace Verification Tests (BR1-SPEC B5 & QS1).
"""
from pathlib import Path
import sys
import uuid
import pytest

_AGENTKNOCK_DIR = Path(__file__).resolve().parent.parent
if str(_AGENTKNOCK_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTKNOCK_DIR))

import agentknock
from agentknock import AgentKnock, configure_default, list_recipients, register_self, send, unregister_self
from agentknock.core.mailbox import Mailbox
from agentknock.core.registry import FileSystemAliasRegistry, InMemoryAliasRegistry
from agentknock.core.probe import AlwaysAliveProbe, FileLockLivenessProbe
from agentknock.testing import assert_send_cannot_reach
from agentknock.mcp_server import dispatch_mcp_tool


def test_facade_di_container(tmp_path):
    """
    AgentKnock DI クラスのテスト:
    Mailbox / Registry / Probe を差し替えて独立稼働できること。
    """
    brain_dir = tmp_path / "brain"
    reg_dir = tmp_path / "registry"

    mailbox = Mailbox(root_dir=brain_dir)
    registry = FileSystemAliasRegistry(registry_dir=reg_dir)
    probe = AlwaysAliveProbe()

    client = AgentKnock(mailbox=mailbox, registry=registry, probe=probe)

    cid = str(uuid.uuid4())
    alias = "worker-agent"

    # 1. 登録
    client.register_self(alias, cid, role="Worker", ttl_sec=3600.0)

    # 2. 一覧
    recipients = client.list_recipients()
    assert len(recipients) == 1
    assert recipients[0]["alias"] == alias
    assert recipients[0]["role"] == "Worker"
    assert "conversation_id" not in recipients[0]

    # 3. 送信
    msg_id = client.send(alias, "Hello from DI client", sender="commander")
    assert msg_id is not None

    undelivered_marker = mailbox.get_undelivered_dir(cid) / msg_id
    assert undelivered_marker.is_file()

    # 4. 登録解除 (Kill Switch)
    ok = client.unregister_self(alias)
    assert ok is True
    assert len(client.list_recipients()) == 0

    with pytest.raises(LookupError):
        client.send(alias, "Should fail", sender="commander")


def test_facade_toplevel_convenience_functions(tmp_path):
    """
    トップレベル糖衣関数のテスト:
    configure_default -> register_self -> list_recipients -> send -> unregister_self
    """
    brain_dir = tmp_path / "brain_toplevel"
    reg_dir = tmp_path / "registry_toplevel"

    mailbox = Mailbox(root_dir=brain_dir)
    registry = FileSystemAliasRegistry(registry_dir=reg_dir)
    probe = AlwaysAliveProbe()

    configure_default(mailbox=mailbox, registry=registry, probe=probe)

    cid = str(uuid.uuid4())
    alias = "echo-agent"

    register_self(alias, cid, role="Echo", ttl_sec=1800.0)

    recs = list_recipients()
    assert any(r["alias"] == alias for r in recs)

    msg_id = send(alias, "Ping", sender="tester")
    assert msg_id is not None
    assert (mailbox.get_undelivered_dir(cid) / msg_id).is_file()

    unreg_ok = unregister_self(alias)
    assert unreg_ok is True
    assert not any(r["alias"] == alias for r in list_recipients())


def test_facade_r2_callgraph_isolation():
    """
    R2 Namespace 不変条件:
    agentknock/__init__.py の send() の AST コールグラフが
    DeliveryAdapter / _win_console / CONIN$ / _adapter 等に到達不能であること。
    """
    init_file = _AGENTKNOCK_DIR / "agentknock" / "__init__.py"
    assert_send_cannot_reach(init_file, entry_func_name="send")


def test_mcp_server_dispatch_v03(tmp_path):
    """
    MCP stdio サーバーディスパッチャの v0.3.0 動作テスト:
    register_self, list_recipients, send, unregister_self
    """
    brain_dir = tmp_path / "brain_mcp"
    reg_dir = tmp_path / "registry_mcp"

    configure_default(
        mailbox=Mailbox(root_dir=brain_dir),
        registry=FileSystemAliasRegistry(registry_dir=reg_dir),
        probe=AlwaysAliveProbe(),
    )

    target_uuid = str(uuid.uuid4())
    alias = "mcp-target"

    # register_self
    reg_res = dispatch_mcp_tool(
        "register_self",
        {"alias": alias, "target_id": target_uuid, "role": "MCP Bot"},
    )
    assert reg_res["status"] == "ok"

    # list_recipients
    list_res = dispatch_mcp_tool("list_recipients", {})
    assert any(r["alias"] == alias for r in list_res["recipients"])

    # send
    send_res = dispatch_mcp_tool(
        "send",
        {"recipient": alias, "content": "Message via MCP", "sender": "mcp-client"},
    )
    assert "message_id" in send_res

    # unregister_self
    unreg_res = dispatch_mcp_tool("unregister_self", {"alias": alias})
    assert unreg_res["status"] == "ok"
    assert unreg_res["unregistered"] is True
